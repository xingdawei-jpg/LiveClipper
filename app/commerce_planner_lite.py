"""Source-only lightweight commercial composition experiment.

Unlike the heavy M2.5 Commerce Director, Lite does not ask a model to first
write a candidate-free commercial script and then ask M2 to fill it.  It
projects facts already present in the immutable Commercial Asset Ledger and
M1 Brief into compact candidate tags, then performs one model ranking call
that produces an ordinary validated NarrativePlan for M3. When the initial
core path is shorter than the requested duration, M2 checks which formal buyer
questions remain unanswered. It then asks the model to recall and choose from
the unconsumed *full* executable-safe pool, one question at a time. This
remains an M2 decision: code exposes the gap and validates the declared
relationship; it never chooses a semantic candidate itself.

The projection never deletes, scores, or reorders candidates.  The model sees
every materializable hard-safe candidate and is the only component that
combines candidate IDs into a commercial sequence.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from ai_cost_ledger import record_ai_call
from ai_model_config import ai_chat_completions_url
from ssl_context import create_ssl_context
from story_planner import (
    CommercialStoryBrief,
    HOOK_PREFERRED_MAX_SECONDS,
    NarrativeBeat,
    NarrativePlan,
    OpeningPackage,
    PlanningCandidate,
    ReplanRequest,
    StoryConsumption,
    _extract_json,
    _parse_beats,
    _parse_depth_expansion,
    _parse_duration_assessment,
    _parse_duration_plan,
    _parse_opening_package,
    _parse_story_consumption,
    bind_story_assets,
    commerce_lite_chapter_saturation_budget,
    commerce_lite_story_budget,
    validate_narrative_plan,
)


COMMERCE_PLANNER_LITE_VERSION = "commerce-planner-lite-v1"
COMMERCE_PLANNER_LITE_STAGE = "M2_5_commerce_lite_ranking"
COMMERCE_LITE_COMPLETION_STAGE = "M2_5_commerce_lite_completion"
COMMERCE_LITE_DRAFT_STAGE = "M2_5_commerce_lite_draft"
COMMERCE_LITE_DRAFT_FINAL_STAGE = "M2_5_commerce_lite_draft_final"
COMMERCE_LITE_RANKING_STAGE = "M2_5_commerce_lite_commercial_ranking"
COMMERCE_LITE_CHAPTER_COMPRESSION_STAGE = "M2_4_chapter_compression"
COMMERCE_LITE_FINAL_EDITOR_STAGE = "M2_final_editor_operations"
# Purchase Journey v1 deliberately replaces the old initial-plan ->
# completion/editor loop only inside the existing experimental M2 route.  It
# is two decisions of the same planner: candidate question tagging/ranking,
# then a declared question route with grounded evidence selection.  It does
# not create a new product-facing layer.
COMMERCE_STRONG_CLIP_RANKING_STAGE = "M2_strong_clip_ranking"
COMMERCE_NARRATIVE_COMPOSITION_STAGE = "M2_purchase_cognition_composition"
COMMERCE_PURCHASE_JOURNEY_RECALL_STAGE = "M2_purchase_journey_targeted_recall"
# This remains an internal M2 decision, after journey coverage is known.  It
# neither discovers a new story nor asks M3 to rescue weak spoken material.
COMMERCE_PURCHASE_JOURNEY_QUALITY_STAGE = "M2_purchase_journey_quality"
COMMERCE_PURCHASE_QUESTION_LOCAL_QUALITY_STAGE = "M2_purchase_question_local_quality"
# P0.3 remains inside the existing M2 route.  It is deliberately a single
# post-quality exploration, not a new planner or a retry loop: only a
# naturally-complete 45–75s narrative may ask whether the unused full pool
# contains genuinely new commercial chapters.
COMMERCE_NARRATIVE_ENRICHMENT_STAGE = "M2_narrative_enrichment"
# P0.4 is one packet decision inside the same M2 route.  It receives locally
# continuous source windows only after P0.2 has already approved a complete
# short cut.  It is deliberately before the P0.3 isolated-candidate fallback.
COMMERCE_CHAPTER_PACKET_STAGE = "M2_chapter_packet_builder"
# P0.5A.3/A.4 Narrative Mode stays inside the existing M2 boundary.  It
# replaces only the older Strong Ranking -> Top12 input path with a source-wide
# calibrated Actor Pool plus an AI-approved Hook -> Payoff opening catalog.
# The Director still owns story intent and the AI Beat Caster still owns every
# semantic selection.
COMMERCE_NARRATIVE_MODE_JOURNEY_STAGE = "M2_narrative_mode_journey"
COMMERCE_NARRATIVE_MODE_BEAT_CASTING_STAGE = "M2_narrative_mode_beat_casting"
COMMERCE_NARRATIVE_MODE_WHOLE_VIDEO_AUDIT_STAGE = "M2_narrative_mode_whole_video_audit"


# This is a small, explicit apparel purchase journey—not a candidate scorer,
# a category keyword filter, or a new planner. It gives the model stable IDs
# for relationships that the M2 contract can audit after selection. Q1/Q2
# establish the core promise; the remaining questions are optional only when
# the full safe pool has real, non-redundant evidence for them.
PURCHASE_JOURNEY_QUESTIONS: tuple[dict[str, Any], ...] = (
    {"purchase_question_id": "Q1", "purchase_question": "为什么想买？它带来什么关键结果？", "journey_role": "core_result", "allowed_answer_roles": ("result", "proof"), "supports_question_id": "", "core": True},
    {"purchase_question_id": "Q2", "purchase_question": "为什么这个结果可信、有效？", "journey_role": "mechanism", "allowed_answer_roles": ("mechanism", "proof"), "supports_question_id": "Q1", "core": True},
    {"purchase_question_id": "Q3", "purchase_question": "我这种身材或尺码能不能穿好？", "journey_role": "fit_or_body_coverage", "allowed_answer_roles": ("result", "proof", "risk_remove"), "supports_question_id": "", "core": False},
    {"purchase_question_id": "Q4", "purchase_question": "夏天或长时间穿舒服吗？", "journey_role": "comfort", "allowed_answer_roles": ("comfort", "proof", "risk_remove"), "supports_question_id": "", "core": False},
    {"purchase_question_id": "Q5", "purchase_question": "穿着有没有实际顾虑需要解除？", "journey_role": "wearing_security", "allowed_answer_roles": ("risk_remove", "proof", "comfort"), "supports_question_id": "", "core": False},
    {"purchase_question_id": "Q6", "purchase_question": "日常怎么穿、怎么搭或适合什么场景？", "journey_role": "styling_or_scene", "allowed_answer_roles": ("scene", "styling", "result", "proof"), "supports_question_id": "", "core": False},
    {"purchase_question_id": "Q7", "purchase_question": "面料或品质为什么值得信任？", "journey_role": "trust", "allowed_answer_roles": ("trust", "proof", "mechanism"), "supports_question_id": "", "core": False},
)
PURCHASE_JOURNEY_BY_ID = {str(item["purchase_question_id"]): item for item in PURCHASE_JOURNEY_QUESTIONS}
MAX_TARGETED_RECALL_PER_QUESTION = 5
# A final buyer-question micro-sequence is intentionally dense: one anchor
# plus at most one *necessary* support.  The director chooses both; this is a
# relation cap that prevents three ways of saying the same Q from becoming
# artificial duration.  A separate buyer question must carry its own value.
MAX_PURCHASE_QUESTION_QUALITY_CANDIDATES = 5
MAX_PURCHASE_QUESTION_QUALITY_SELECTED_CANDIDATES = 2
# This is intentionally separate from the P0.2 per-question cap above.
# An Enrichment chapter may retain its clean anchor plus at most two M2-chosen
# supports.  A fourth line is an explicitly explained exception, never a
# programmatic duration fill.
MAX_NARRATIVE_ENRICHMENT_CANDIDATES_PER_CHAPTER = 3
MAX_NARRATIVE_ENRICHMENT_EXCEPTION_CANDIDATES_PER_CHAPTER = 4
NARRATIVE_ENRICHMENT_ALLOWED_ANSWER_ROLES = frozenset({
    "result", "mechanism", "proof", "risk_remove", "comfort", "scene", "styling", "trust",
})
# Source-window sizing is transport configuration rather than a commercial
# rule.  It merely restores the local live explanation that candidate slicing
# hid; M2 still decides whether any line belongs in a final Packet.
CHAPTER_PACKET_CONTEXT_BEFORE_SECONDS = 12.0
CHAPTER_PACKET_CONTEXT_AFTER_SECONDS = 12.0
CHAPTER_PACKET_DISCOVERY_GAP_SECONDS = 8.0
CHAPTER_PACKET_DISCOVERY_MAX_SECONDS = 32.0
MAX_CHAPTER_PACKET_DISCOVERY_WINDOWS = 12
# 45–75s is the only range where the Director Blueprint can ask the existing
# M2 Purchase Journey to look beyond a compact conversion path.  This is a
# soft-depth switch, never a duration quota or a separate planner.
NARRATIVE_DEPTH_TARGET_MIN_SECONDS = 45.0
NARRATIVE_DEPTH_TARGET_MAX_SECONDS = 75.0
# A short but complete result sentence can be a stronger short-video hook than
# a padded live-stream sentence.  M3 only records a sub-preferred hook length
# as a review signal; it does not make 2.5 seconds a hard word-lineage rule.
# Keep this as an M2 eligibility floor, not a reason to throw away a clean
# 1.9-second result and retain a longer malformed sentence.
QUALITY_OPENING_MIN_SECONDS = 1.5
QUALITY_OPENING_LIVE_LEADINS = ("好的", "来", "的确", "你看", "它其实", "有没有觉得", "是不是")
# These are explicit opening-only rejections from the focused caramel review.
# They block a declared opening but never cause code to select a substitute.
QUALITY_OPENING_REJECT_MARKERS = ("大斜方肌",)
# These are observed, plainly unplayable raw-ASR/live-residue fragments in
# the focused 焦糖 regression.  This is a rejection list, not a semantic
# scorer: it can only block an AI-declared final utterance, never pick a
# substitute sentence.
QUALITY_FINAL_UTTERANCE_REJECT_MARKERS = (
    "像100斤葡萄", "35厘米", "A类母婴店", "这个人间一定是直角",
    "大斜方间", "看到吧", "不显得人非常的窄", "你想要在", "上身完全不",
    # 焦糖 P0.2 的已核验反例：这是一个未回答的直播反问，不是可发布口播。
    "肩干嘛",
)
# These are not a semantic quality model.  They are the explicit, surface-form
# live-interaction openings the focused review ruled out for *any* final spoken
# line, not merely for the opening.  The director still selects among the
# remaining real candidates; this only prevents a known dependent fragment from
# posing as an independent utterance.
QUALITY_FINAL_UTTERANCE_INTERACTION_LEADINS = ("有没有觉得", "是不是", "你看", "看到了吗")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _quality_utterance_reject_marker(text: str) -> str:
    normalized = re.sub(r"\s+", "", text or "")
    return next((marker for marker in QUALITY_FINAL_UTTERANCE_REJECT_MARKERS if marker in normalized), "")


def _quality_final_utterance_reject_reason(text: str) -> str:
    """Return an explicit non-semantic hard block for a final spoken line."""
    marker = _quality_utterance_reject_marker(text)
    if marker:
        return marker
    opening = re.sub(r"^[\s，。！？、…]+", "", text or "")
    leadin = next((prefix for prefix in QUALITY_FINAL_UTTERANCE_INTERACTION_LEADINS if opening.startswith(prefix)), "")
    return f"live_interaction:{leadin}" if leadin else ""


def _quality_opening_reject_marker(text: str) -> str:
    normalized = re.sub(r"\s+", "", text or "")
    return next((marker for marker in QUALITY_OPENING_REJECT_MARKERS if marker in normalized), "")


def _quality_selected_candidate_ids(raw_row: Mapping[str, Any]) -> tuple[int, ...]:
    """Read a Quality micro-sequence while accepting the previous one-ID form.

    The first ID is the director's anchor.  Additional IDs are never inferred
    locally: they are only the M2-declared, differently-roled support lines.
    """
    selected = _as_int_tuple(raw_row.get("selected_candidate_ids"))
    if selected:
        return selected
    legacy = _as_int_tuple(raw_row.get("selected_candidate_id"))
    return legacy[:1]


def _purchase_journey_question_payload(
    question_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return contract fields for the requested formal buyer questions."""
    requested = set(PURCHASE_JOURNEY_BY_ID if question_ids is None else question_ids)
    return [
        {
            "purchase_question_id": item["purchase_question_id"],
            "purchase_question": item["purchase_question"],
            "journey_role": item["journey_role"],
            "allowed_answer_roles": list(item["allowed_answer_roles"]),
            "supports_question_id": item["supports_question_id"],
            "core": bool(item["core"]),
        }
        for item in PURCHASE_JOURNEY_QUESTIONS
        if str(item["purchase_question_id"]) in requested
    ]


def _narrative_depth_blueprint(
    director_strategy_contract: Mapping[str, Any] | None, *, target_duration: float,
) -> dict[str, Any] | None:
    """Read a P0 Director Blueprint only inside the narrative-depth range.

    The returned slots retain their formal purchase-question and allowed-role
    relationship.  Code never turns a slot into a candidate shortlist or
    chooses an utterance; it merely tells the existing targeted-recall call
    which independent buyer values remain unexplored.
    """
    if not (NARRATIVE_DEPTH_TARGET_MIN_SECONDS <= float(target_duration) <= NARRATIVE_DEPTH_TARGET_MAX_SECONDS):
        return None
    contract = dict(director_strategy_contract or {})
    archetype = _text(contract.get("narrative_archetype")).lower()
    blueprint = contract.get("blueprint")
    if archetype not in {"pain_point", "scene_immersion"} or not isinstance(blueprint, Mapping):
        return None
    seen_questions: set[str] = set()
    slots: list[dict[str, Any]] = []
    for raw in blueprint.get("chapter_slots") or ():
        if not isinstance(raw, Mapping):
            continue
        question_id = _text(raw.get("purchase_question_id"))
        formal = PURCHASE_JOURNEY_BY_ID.get(question_id)
        if formal is None or question_id in seen_questions:
            continue
        allowed = tuple(
            role for role in (_text(item).lower() for item in raw.get("answer_roles") or ())
            if role in set(formal.get("allowed_answer_roles") or ())
        )
        if not allowed:
            continue
        seen_questions.add(question_id)
        slots.append({
            "slot_id": _text(raw.get("slot_id")) or f"depth_{question_id.lower()}",
            "priority": max(1, int(_number(raw.get("priority")) or len(slots) + 1)),
            "phase": _text(raw.get("phase")) or "depth",
            "coverage": _text(raw.get("coverage")).lower() or "recommended",
            "purchase_question_id": question_id,
            "purchase_question": _text(formal.get("purchase_question")),
            "journey_role": _text(formal.get("journey_role")),
            "supports_question_id": _text(formal.get("supports_question_id")),
            "allowed_answer_roles": list(dict.fromkeys(allowed)),
            "core": _text(raw.get("coverage")).lower() == "required",
        })
    if not slots:
        return None
    slots.sort(key=lambda item: (int(item["priority"]), item["slot_id"]))
    return {
        "narrative_archetype": archetype,
        "core_desire": _text(contract.get("core_desire")),
        "opening_promise": _text(contract.get("opening_promise")),
        "opening_scope": dict(contract.get("opening_scope") or {}),
        "early_journey_scope": dict(contract.get("early_journey_scope") or {}),
        "blueprint_version": _text(blueprint.get("version")),
        "chapter_slots": slots,
        "target_duration": float(target_duration),
        "duration_policy": _text(blueprint.get("duration_policy")) or "soft_target_no_padding",
        "stop_rule": _text(blueprint.get("stop_rule")),
    }


def _blueprint_missing_purchase_questions(
    path_audit: Mapping[str, Any], blueprint: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Detect only Archetype chapters still worth a full-pool exploration.

    ``optional`` is visible to M2 but never creates a completion obligation.
    This is deliberately a coverage decision, not a candidate decision.
    """
    relations = [
        item for item in path_audit.get("candidate_relations") or ()
        if isinstance(item, Mapping)
    ]
    missing: list[dict[str, Any]] = []
    for slot in blueprint.get("chapter_slots") or ():
        if not isinstance(slot, Mapping):
            continue
        if _text(slot.get("coverage")).lower() == "optional":
            continue
        question_id = _text(slot.get("purchase_question_id"))
        allowed_roles = {
            _text(role).lower() for role in slot.get("allowed_answer_roles") or () if _text(role)
        }
        covered = any(
            _text(item.get("purchase_question_id")) == question_id
            and _text(item.get("answer_role")).lower() in allowed_roles
            for item in relations
        )
        if not covered:
            missing.append(dict(slot))
    return missing


def _recall_question_payload(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep formal and Blueprint recall requirements auditable verbatim."""
    return [
        {
            "purchase_question_id": _text(item.get("purchase_question_id")),
            "purchase_question": _text(item.get("purchase_question")),
            "journey_role": _text(item.get("journey_role")),
            "allowed_answer_roles": list(item.get("allowed_answer_roles") or ()),
            "supports_question_id": _text(item.get("supports_question_id")),
            "core": bool(item.get("core")),
            **({
                "blueprint_slot_id": _text(item.get("slot_id")),
                "blueprint_priority": int(_number(item.get("priority")) or 0),
                "blueprint_phase": _text(item.get("phase")),
                "blueprint_coverage": _text(item.get("coverage")),
            } if _text(item.get("slot_id")) else {}),
        }
        for item in items
    ]


def _rank_answer_role(rank: Any) -> str:
    """Read the formal role while keeping old ranking reports readable."""
    return _text(getattr(rank, "answer_role", "")) or _text(
        getattr(rank, "evidence_function", "")
    ) or _text(getattr(rank, "purchase_question_role", ""))


def _rank_purchase_question(rank: Any) -> str:
    return _text(getattr(rank, "purchase_question", "")) or _text(
        getattr(rank, "answered_question", "")
    )


@dataclass(frozen=True)
class CommerceLiteTag:
    """A compact, read-only description of one legal candidate.

    ``asset_role`` and ``story_permission`` originate from the Ledger.  M1
    roles/tier are references, not an allow-list.  ``text`` remains present so
    the ranker must ground every choice in actual spoken evidence.
    """

    candidate_id: int
    text: str
    duration: float
    asset_role: str
    story_permission: str
    product_focus: str
    m1_tiers: tuple[str, ...]
    m1_roles: tuple[str, ...]
    m1_claims: tuple[str, ...]
    purchase_value_hints: tuple[str, ...]
    visual_role_hints: tuple[str, ...]
    visual_role_provenance: str
    hook_eligible: bool
    materializable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "text": self.text,
            "duration": round(float(self.duration), 3),
            "asset_role": self.asset_role,
            "story_permission": self.story_permission,
            "product_focus": self.product_focus,
            "m1_tiers": list(self.m1_tiers),
            "m1_roles": list(self.m1_roles),
            "m1_claims": list(self.m1_claims),
            "purchase_value_hints": list(self.purchase_value_hints),
            # No frame-level vision claim is made here.  These are only
            # read-only text/asset cues for M2 to distinguish an explanation
            # from a likely visual proof opportunity.
            "visual_role_hints": list(self.visual_role_hints),
            "visual_role_provenance": self.visual_role_provenance,
            "hook_eligible": self.hook_eligible,
            "materializable": self.materializable,
            "lineage_note": "read_only_tag_not_a_candidate_filter_or_local_rank",
        }


def _quality_candidate_pool_item(tag: CommerceLiteTag) -> dict[str, Any]:
    """Expose final-utterance hard blocks to M2 without choosing a substitute.

    Every materializable candidate remains visible for audit.  The additional
    fields only distinguish a literal, previously observed ASR/live-residue
    rejection from an otherwise selectable original utterance.
    """
    item = tag.to_dict()
    reason = _quality_final_utterance_reject_reason(tag.text)
    opening_text = re.sub(r"^[\s，。！？、…]+", "", tag.text or "")
    opening_leadin = next((
        prefix for prefix in QUALITY_OPENING_LIVE_LEADINS
        if opening_text.startswith(prefix)
    ), "")
    opening_reason = (
        reason
        or ("hook_eligible=false" if not tag.hook_eligible else "")
        or ("duration_below_1.5s" if float(tag.duration) < QUALITY_OPENING_MIN_SECONDS else "")
        or (f"live_leadin:{opening_leadin}" if opening_leadin else "")
        or _quality_opening_reject_marker(tag.text)
    )
    item.update({
        "final_utterance_hard_blocked": bool(reason),
        "final_utterance_hard_block_reason": reason,
        "final_utterance_selection_status": "blocked" if reason else "selectable",
        # This restates the existing Hook contract at candidate level.  It is
        # not a hook scorer and never nominates an opening; M2 still compares
        # every eligible original utterance and selects the anchor itself.
        "q1_opening_anchor_eligible": not bool(opening_reason),
        "q1_opening_anchor_block_reason": opening_reason,
    })
    return item


@dataclass(frozen=True)
class CommerceLiteDraftBeat:
    """A candidate-free buying-decision step used only by the offline experiment."""

    draft_id: str
    purchase_journey_role: str
    buyer_question: str
    purchase_value_domain: str
    purchase_value_outcomes: tuple[str, ...]
    decision_reason: str
    suggested_seconds: float
    priority: str
    evidence_source_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "purchase_journey_role": self.purchase_journey_role,
            "buyer_question": self.buyer_question,
            "purchase_value_domain": self.purchase_value_domain,
            "purchase_value_outcomes": list(self.purchase_value_outcomes),
            "decision_reason": self.decision_reason,
            "suggested_seconds": self.suggested_seconds,
            "priority": self.priority,
            # Provenance only: this is neither an edit list nor M3 authority.
            "evidence_source_ids": list(self.evidence_source_ids),
        }


@dataclass(frozen=True)
class CommerceLiteDraft:
    """A non-executable commercial outline; it deliberately carries no clip IDs."""

    strategy_id: str
    target_duration: float
    buying_path: tuple[CommerceLiteDraftBeat, ...]
    draft_rationale: str
    suggested_duration: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "target_duration": self.target_duration,
            "draft_rationale": self.draft_rationale,
            "suggested_duration": self.suggested_duration,
            "buying_path": [beat.to_dict() for beat in self.buying_path],
            "executable": False,
            "boundary": "candidate_free_commercial_thinking_only_not_m3_input",
        }


@dataclass(frozen=True)
class CommerceLiteRankedValue:
    """One commercial decision retained from a Draft; never a clip selection."""

    rank: int
    draft_ids: tuple[str, ...]
    claim: str
    commercial_role: str
    retain_reason: str
    evidence_source_ids: tuple[int, ...]
    proof_budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "draft_ids": list(self.draft_ids),
            "claim": self.claim,
            "commercial_role": self.commercial_role,
            "retain_reason": self.retain_reason,
            "evidence_source_ids": list(self.evidence_source_ids),
            "proof_budget": self.proof_budget,
        }


@dataclass(frozen=True)
class CommerceLiteCommercialRanking:
    """A capped purchase-value priority map between Draft and Final."""

    strategy_id: str
    retained_values: tuple[CommerceLiteRankedValue, ...]
    dropped_draft_ids: tuple[str, ...]
    dropped_reasons: Mapping[str, str]
    ranking_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "retained_values": [item.to_dict() for item in self.retained_values],
            "dropped_draft_ids": list(self.dropped_draft_ids),
            "dropped_reasons": dict(self.dropped_reasons),
            "ranking_reason": self.ranking_reason,
            "executable": False,
            "boundary": "purchase_value_priority_only_not_candidate_filter_or_m3_input",
        }


@dataclass(frozen=True)
class CommerceLiteProofDecision:
    candidate_id: int
    evidence_role: str

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "evidence_role": self.evidence_role}


@dataclass(frozen=True)
class CommerceLiteProofAllocation:
    value_rank: int
    selected_proofs: tuple[CommerceLiteProofDecision, ...]
    discarded_proof_reasons: Mapping[int, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "value_rank": self.value_rank,
            "selected_proofs": [item.to_dict() for item in self.selected_proofs],
            "discarded_proofs": [
                {"candidate_id": candidate_id, "discard_reason": reason}
                for candidate_id, reason in self.discarded_proof_reasons.items()
            ],
        }


@dataclass(frozen=True)
class StrongClipRank:
    """Read-only editorial assessment of one real, strategy-related clip."""

    candidate_id: int
    rank: int
    standalone_strength: float
    hook_power: float
    purchase_value: str
    purchase_outcome: str
    purchase_question_id: str
    purchase_question: str
    supports_question_id: str
    answer_role: str
    # Legacy aliases remain in the serialized record so existing M2 reports
    # and validators continue to read prior experiments. New M2 logic uses
    # the explicit question/support/answer relationship above.
    purchase_question_role: str
    answered_question: str
    evidence_function: str
    proof_strength: float
    redundancy_group: str
    fragment: bool
    visual_dependency: bool
    opening_rank: int
    opening_reason: str
    selection_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "rank": self.rank,
            "standalone_strength": self.standalone_strength,
            "hook_power": self.hook_power,
            "purchase_value": self.purchase_value,
            # These are editorial annotations for the selected strategy.  They
            # do not mutate the Candidate Ledger or create a candidate filter.
            "purchase_outcome": self.purchase_outcome,
            "purchase_question_id": self.purchase_question_id,
            "purchase_question": self.purchase_question,
            "supports_question_id": self.supports_question_id,
            "answer_role": self.answer_role,
            "purchase_question_role": self.purchase_question_role,
            "answered_question": self.answered_question,
            "evidence_function": self.evidence_function,
            "proof_strength": self.proof_strength,
            "redundancy_group": self.redundancy_group,
            "fragment": self.fragment,
            "visual_dependency": self.visual_dependency,
            "opening_rank": self.opening_rank,
            "opening_reason": self.opening_reason,
            "selection_reason": self.selection_reason,
        }


def _as_text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    return tuple(dict.fromkeys(_text(item).lower() for item in value if _text(item)))


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int_tuple(value: Any) -> tuple[int, ...]:
    if isinstance(value, (int, float, str)):
        value = (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    result: list[int] = []
    for item in value:
        try:
            candidate_id = int(item)
        except (TypeError, ValueError):
            continue
        if candidate_id > 0 and candidate_id not in result:
            result.append(candidate_id)
    return tuple(result)


def _m1_facts_by_candidate(strategy: Any) -> dict[int, list[dict[str, str]]]:
    facts: dict[int, list[dict[str, str]]] = {}
    for attr, tier in (
        ("core_evidence_pool", "core"),
        ("supporting_evidence_pool", "supporting"),
        ("bridge_candidates", "bridge"),
    ):
        for evidence in tuple(getattr(strategy, attr, ()) or ()):
            role = _text(getattr(evidence, "role", ""))
            claim = _text(getattr(evidence, "claim", ""))
            for raw_id in tuple(getattr(evidence, "subtitle_ids", ()) or ()):
                try:
                    candidate_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                facts.setdefault(candidate_id, []).append({
                    "tier": tier,
                    "role": role,
                    "claim": claim,
                })
    return facts


def _purchase_value_hints(asset_role: str, m1_roles: Sequence[str]) -> tuple[str, ...]:
    """Expose existing asset semantics as hints, never as a candidate filter."""
    hints: list[str] = []
    for value in m1_roles:
        hint = {
            "hook": "core_promise",
            "mechanism": "design_mechanism",
            "proof": "visible_result",
            "result": "visible_result",
            "benefit": "buyer_benefit",
            "scene": "scene_imagination",
            "trust": "comfort_or_trust",
        }.get(_text(value).lower(), "")
        if hint and hint not in hints:
            hints.append(hint)
    asset_hint = {
        "product_proof": "product_proof",
        "design_explanation": "design_mechanism",
        "wearing_effect": "visible_result",
        "styling_scene": "styling_outcome",
        "lifestyle_scene": "scene_imagination",
        "trust_signal": "comfort_or_trust",
    }.get(_text(asset_role).lower(), "")
    if asset_hint and asset_hint not in hints:
        hints.append(asset_hint)
    return tuple(hints)


def _visual_role_hints(
    *, asset_role: str, m1_roles: Sequence[str],
) -> tuple[str, ...]:
    """Return conservative visual-role *hints*, never claimed visual facts.

    The experimental path currently has no frame analysis attached to frozen
    candidates.  Existing Ledger and M1 facts can still say what sort of
    proof the spoken line is suited to support.  ``talking_only`` remains the
    default so M2 cannot pretend that a material claim includes a hand-feel
    demo or that a fit claim includes a turn-around shot.
    """
    hints: list[str] = []
    by_asset = {
        "wearing_effect": "result_show",
        "design_explanation": "detail_proof",
        "styling_scene": "styling",
        "lifestyle_scene": "outfit_complete",
    }
    by_role = {
        "proof": "result_show",
        "result": "result_show",
        "mechanism": "detail_proof",
        "scene": "outfit_complete",
    }
    asset_hint = by_asset.get(_text(asset_role).lower(), "")
    if asset_hint:
        hints.append(asset_hint)
    for role in m1_roles:
        hint = by_role.get(_text(role).lower(), "")
        if hint and hint not in hints:
            hints.append(hint)

    # Deliberately do not add product-category keywords here.  Without an
    # actual video-sidecar, a phrase such as “look here” is not evidence that
    # the frame contains a turn, material demo, or body comparison.
    return tuple(hints) if hints else ("talking_only",)


def build_commerce_lite_tags(
    *,
    strategy: Any,
    safe_candidates: Sequence[PlanningCandidate],
    ledger_assets: Sequence[Mapping[str, Any]],
    executable_evidence: Mapping[int, Mapping[str, Any]],
) -> tuple[CommerceLiteTag, ...]:
    """Project existing facts for every materializable hard-safe candidate.

    The function does not use candidate text to make an eligibility judgement;
    candidates come from the pre-existing Executable Evidence View.  Absence
    of an M1 link is recorded as ``m1_tiers=[]`` rather than hiding reserve
    material from the ranker.
    """
    ledger_by_id = {
        int(item.get("candidate_id") or 0): item
        for item in ledger_assets
        if isinstance(item, Mapping) and int(item.get("candidate_id") or 0) > 0
    }
    m1_by_id = _m1_facts_by_candidate(strategy)
    tags: list[CommerceLiteTag] = []
    for candidate in safe_candidates:
        facts = dict(executable_evidence.get(candidate.candidate_id) or {})
        asset = dict(ledger_by_id.get(candidate.candidate_id) or {})
        relation = asset.get("subject_context")
        relation = dict(relation) if isinstance(relation, Mapping) else {}
        linked = m1_by_id.get(candidate.candidate_id, [])
        tags.append(CommerceLiteTag(
            candidate_id=candidate.candidate_id,
            text=candidate.text,
            duration=candidate.duration,
            asset_role=_text(asset.get("asset_role")) or "unknown",
            story_permission=_text(asset.get("story_permission")) or "supporting_story",
            product_focus=_text(relation.get("product_focus")) or "unknown",
            m1_tiers=tuple(dict.fromkeys(value["tier"] for value in linked)),
            m1_roles=tuple(dict.fromkeys(value["role"] for value in linked if value["role"])),
            m1_claims=tuple(dict.fromkeys(value["claim"] for value in linked if value["claim"])),
            purchase_value_hints=_purchase_value_hints(
                _text(asset.get("asset_role")),
                tuple(value["role"] for value in linked if value["role"]),
            ),
            visual_role_hints=_visual_role_hints(
                asset_role=_text(asset.get("asset_role")),
                m1_roles=tuple(value["role"] for value in linked if value["role"]),
            ),
            visual_role_provenance="ledger_m1_text_hint_not_frame_vision",
            hook_eligible=bool(candidate.hook_eligible),
            materializable=bool(facts.get("materializable", True)),
        ))
    return tuple(tags)


def _compact_story(strategy: Any) -> dict[str, Any]:
    """Keep only the M1 facts necessary to prevent theme rediscovery."""
    brief = CommercialStoryBrief.from_strategy(strategy)
    return {
        "strategy_id": brief.strategy_id,
        "story_priority": brief.story_priority,
        "thesis": brief.thesis,
        "audience_tension": brief.audience_tension,
        "transformation": brief.transformation,
        "product_role": brief.product_role,
        "payoff": brief.payoff,
        "supporting_arcs": list(brief.supporting_arcs),
    }


def build_commerce_lite_ranking_prompt(
    *,
    strategy: Any,
    tags: Sequence[CommerceLiteTag],
    target_duration: float,
    selection_contract: Mapping[str, Any] | None,
) -> str:
    """Request one grounded candidate composition, not a separate screenplay."""
    tag_payload = [tag.to_dict() for tag in tags]
    story_budget = commerce_lite_story_budget(target_duration)
    return "\n".join((
        "你是 Commercial Planner Lite：从完整的、已 hard-safe 且可物化候选中组成一条购买认知路径。",
        "这不是重型 Director：不要先重写广告脚本，不要生成卖点清单，不要创造事实；只组合候选 ID。",
        "必须执行同一个 M1 story，不得另起价格、折扣、库存、福利、其他商品等主题。",
        "候选标签是只读资产事实，不是候选白名单：所有列出的 candidate_id 都合法，M1 未关联的 reserve 仍可在服务同一故事时使用。",
        "组合目标是同一主故事下的购买价值增量，不是章节类型覆盖。identity/difference/benefit/proof/scene "
        "这些结构标签不同，不代表消费者获得了不同购买理由。",
        "C1+C2 可以共同建立主承诺和设计机制；从 C3 起，每章先回答：相对前文，它新增了什么购买理由？"
        "优先扩展新人群或身材包容、舒适度、穿着安全、搭配结果、场景想象、信任或使用周期。",
        "每章必须声明 purchase_value_domain（购买结果域）和 purchase_value_outcomes（一个或多个具体购买结果）。"
        "例如 body_appearance 域下可分别是 shoulder_narrowing、waist_definition、hip_coverage、"
        "body_slimming、size_inclusion；它们是不同顾虑，不能因为同属一个域就当成重复。",
        "purchase_value_dimension 只允许写 new_outcome 或 same_claim_additional_proof。"
        "同一具体 outcome 只能出现一次 new_outcome；若只是为已建立 outcome 增加更强证据，写 "
        "same_claim_additional_proof，且 outcomes 必须引用已出现的同一 outcome，整条片最多一章，不能用来凑时长。",
        "每章还必须填写 purchase_value_reason（一句说明新增或补强的消费者购买理由）。"
        "不要用近义词把同一结果伪装成不同 outcome；判断标准是这一章是否改变消费者的购买理由。",
        "选择优先级：不同购买价值的推进优先于逐个罗列卖点，也优先于同一 claim 的多次证明。",
        "你有删除权：候选可用不等于必须进入成片。省略低商业优先级素材是正确决定；不要为了使用更多素材而拉长片子。",
        "先输出一个最小完整主故事，最多 5 章；若它仍自然偏短，后续 Completion Pass 才能在不改你已有章节的前提下追加缺失购买环节，最终最多 7 章。",
        "本次必须遵守商业片节奏预算。核心故事（痛点/机制/可见结果/身材延展）至少占实际时长 50%，"
        "辅助信息合计不得超过 50%。总章节、总候选和每类辅助信息的上限见下方预算。",
        "章节内部必须只回答一个消费者购买疑问：舒适、信任、尺码、搭配、耐穿等辅助章节最多选 1 个候选；"
        "不要把多条近义证明放进同一章凑时长。身材延展最多 2 个候选；核心痛点/机制/结果章最多 3 个候选。",
        "如果一个候选已经充分回答问题，后续同类素材必须舍弃。候选文本里有多个事实，也只以本章新增的主要购买结果声明 outcomes。",
        "若无法在预算内自然完成故事，宁可诚实报告 insufficient_material，也不得输出超预算的长片。",
        "第一章必须是独立、完整、<=8秒的 Hook；第二章立即兑现它。禁止残句、直播过程话、候选复用。",
        "若素材不足目标时长，必须诚实报告 insufficient_material，不能重复同一 claim 或本地补片。",
        f"目标时长约 {max(1.0, float(target_duration)):.1f}s。",
        "章节预算：",
        json.dumps(story_budget, ensure_ascii=False, separators=(",", ":")),
        "M1 固定商业故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "Selection Contract：",
        json.dumps(dict(selection_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "完整候选商业标签（每一条均保留原字幕文本与 ID）：",
        json.dumps(tag_payload, ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "opening_package": {
                "hook_promise": "", "payoff_delivery": "", "connection_reason": "",
                "hook_candidate_ids": [], "payoff_candidate_ids": [], "hook_integrity_reason": "",
            },
            "chapters": [{
                "chapter_id": "C1", "narrative_role": "hook", "goal": "", "candidate_ids": [],
                "asset_tier": "core/supporting/bridge/safe_reserve", "story_support": "",
                "commerce_beat_id": "identity/difference/problem_or_benefit/visible_result/proof/scene_or_audience/trust",
                "value_dimension": "", "purchase_value_dimension": "new_outcome/same_claim_additional_proof",
                "purchase_value_domain": "", "purchase_value_outcomes": [], "purchase_value_reason": "",
            }],
            "story_consumption": {
                "hero_strategy_id": "", "hero_priority": "", "hero_consistency_reason": "",
                "supporting_chapter_ids": [], "bridge_chapter_ids": [], "no_rediscovery": True,
                "supporting_candidate_ids": [], "bridge_candidate_ids": [],
            },
            "duration_assessment": {"status": "full/insufficient_material", "reason": ""},
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def build_commerce_lite_draft_prompt(
    *, strategy: Any, tags: Sequence[CommerceLiteTag], target_duration: float,
) -> str:
    """Plan a complete buying path before candidate selection or compression.

    This is intentionally not a ``NarrativePlan`` and contains no clip order,
    timestamps, chapters, or opening declaration. Each step carries only
    read-only evidence provenance so unsupported commercial ideas are visible
    before the following Final pass applies the strict executable contract.
    It can never be passed to M3.
    """
    return "\n".join((
        "你在做 Commercial Planner Lite 的 Draft 阶段：先想清楚一条商品成交路径，再进入候选选择与压缩。",
        "这不是片单，也不是最终计划：不得输出字幕、时间轴、开场候选或章节选片。Draft 永远不能进入 M3。",
        "固定 M1 商业故事，不得另起价格、优惠、库存、福利或其他商品主题。",
        "先列出完整但克制的购买决策路径：观众为什么停留、产品如何解决、看见什么结果、谁能穿、舒适或信任、",
        "以及需要什么风险解除或场景。允许 7-12 个决策步骤、约45-70秒的自然讲解容量；允许暂时有相邻证明，",
        "因为下一阶段会压缩重复、删除低价值步骤并补足有真实证据的缺口。",
        "每一步只描述一个消费者决策问题和一个购买结果。不要把弹力/柔软/透气拆成三个卖点章节；它们应服务于",
        "同一个问题，例如‘穿起来会不会难受’。",
        "每一步必须写 evidence_source_ids：它只是这一步的真实事实来源编号，不是最终选片、顺序或时间轴。",
        "只能规划下面真实资产可能支持的购买价值；如果没有证据，不要把洗后、退换、尺码、安全、发货或使用周期硬写进路径。",
        f"目标成片时长 {max(1.0, float(target_duration)):.1f}s。",
        "M1 固定商业故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "候选资产地图（只供判断可被哪些真实事实支持，不是让你选片）：",
        json.dumps([tag.to_dict() for tag in tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "draft_rationale": "",
            "suggested_duration": 45.0,
            "buying_path": [{
                "draft_id": "D1", "purchase_journey_role": "problem/mechanism/result/body_fit/comfort/trust/risk_reduction/scene",
                "buyer_question": "", "purchase_value_domain": "", "purchase_value_outcomes": [],
                "decision_reason": "", "suggested_seconds": 5.0, "priority": "hero/supporting", "evidence_source_ids": [],
            }],
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _parse_commerce_lite_draft(data: Mapping[str, Any], *, strategy: Any, target_duration: float) -> CommerceLiteDraft:
    raw_path = data.get("buying_path") or data.get("draft_story") or data.get("steps") or ()
    if isinstance(raw_path, Mapping):
        raw_path = (raw_path,)
    beats: list[CommerceLiteDraftBeat] = []
    for index, item in enumerate(raw_path, 1):
        if not isinstance(item, Mapping):
            continue
        beats.append(CommerceLiteDraftBeat(
            draft_id=_text(item.get("draft_id")) or f"D{index}",
            purchase_journey_role=_text(item.get("purchase_journey_role") or item.get("role")).lower(),
            buyer_question=_text(item.get("buyer_question")),
            purchase_value_domain=_text(item.get("purchase_value_domain")).lower(),
            purchase_value_outcomes=_as_text_tuple(item.get("purchase_value_outcomes")),
            decision_reason=_text(item.get("decision_reason") or item.get("reason")),
            suggested_seconds=round(max(0.0, _number(item.get("suggested_seconds"))), 3),
            priority=_text(item.get("priority")).lower(),
            evidence_source_ids=_as_int_tuple(item.get("evidence_source_ids")),
        ))
    if not beats:
        raise ValueError("Commerce Planner Lite Draft 没有返回购买路径")
    return CommerceLiteDraft(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        target_duration=float(target_duration),
        buying_path=tuple(beats),
        draft_rationale=_text(data.get("draft_rationale")),
        suggested_duration=round(max(0.0, _number(data.get("suggested_duration"))), 3),
    )


def _draft_grounding_audit(draft: CommerceLiteDraft, tags: Sequence[CommerceLiteTag]) -> dict[str, Any]:
    """Audit Draft factual references without filtering or rewriting it."""
    available = {tag.candidate_id for tag in tags if tag.materializable}
    rows: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for beat in draft.buying_path:
        source_ids = set(beat.evidence_source_ids)
        unknown = sorted(source_ids - available)
        if not source_ids or unknown:
            unsupported.append(beat.draft_id)
        rows.append({
            "draft_id": beat.draft_id,
            "evidence_source_ids": sorted(source_ids),
            "unknown_source_ids": unknown,
            "grounded": bool(source_ids) and not unknown,
        })
    return {
        "grounded": not unsupported,
        "unsupported_draft_ids": unsupported,
        "steps": rows,
        "boundary": "audit_only_no_draft_or_final_selection_mutation",
    }


def build_commerce_lite_commercial_ranking_prompt(
    *, strategy: Any, draft: CommerceLiteDraft, tags: Sequence[CommerceLiteTag],
) -> str:
    """Rank buying values before Final decides which evidence to keep."""
    return "\n".join((
        "你在做 Commercial Value Ranking：这是 Draft 与 Final 之间的商业取舍，不是选片。",
        "从 Draft 的购买路径中最多保留5个最值得在60秒内讲的购买价值；其余写入 dropped_draft_ids 并说明为什么可以牺牲。",
        "排序优先级是：核心购买阻力及解决方案 > 可见证据 > 关键风险解除 > 与主故事直接相关的舒适/信任 > 场景。",
        "真实但低优先级的信息可以淘汰；例如物流、预售、直播流程、泛化介绍，除非它直接解除当前主故事的购买阻力。",
        "不要因为 Draft 有10步就保留10个主题；也不要因为‘面料/安全/舒适’词不同就拆成多个同等优先级理由。",
        "若 M1 有 Bridge，只有它能推进已保留购买路径时才保留为 commercial_role=bridge；否则明确淘汰原因。",
        "每个 retained value 必须引用 Draft 中已有的 evidence_source_ids，并给出 proof_budget：核心购买价值可为2-3，辅助价值为1。",
        "这些只是事实血缘，仍不是 Final 的候选白名单；proof_budget 是 Final 可用的最大证明数量，不是要求把证据用满。",
        "固定 M1 故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "Draft（不可执行）：",
        json.dumps(draft.to_dict(), ensure_ascii=False, separators=(",", ":")),
        "候选资产地图（只供判断事实和 Bridge，不用于选片）：",
        json.dumps([tag.to_dict() for tag in tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "ranking_reason": "",
            "retained_values": [{
                "rank": 1, "draft_ids": ["D1"], "claim": "", "commercial_role": "pain_solution/proof/risk_removal/purchase_barrier/trust/bridge",
                "retain_reason": "", "evidence_source_ids": [], "proof_budget": 2,
            }],
            "dropped_draft_ids": [{"draft_id": "D6", "reason": ""}],
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _parse_commerce_lite_ranking(
    data: Mapping[str, Any], *, strategy: Any, draft: CommerceLiteDraft,
) -> CommerceLiteCommercialRanking:
    raw_values = data.get("retained_values") or ()
    if isinstance(raw_values, Mapping):
        raw_values = (raw_values,)
    values: list[CommerceLiteRankedValue] = []
    for index, item in enumerate(raw_values, 1):
        if not isinstance(item, Mapping):
            continue
        raw_ids = item.get("draft_ids") or item.get("draft_id") or ()
        if isinstance(raw_ids, str):
            raw_ids = (raw_ids,)
        draft_ids = tuple(dict.fromkeys(_text(value) for value in raw_ids if _text(value))) if isinstance(raw_ids, Sequence) else ()
        values.append(CommerceLiteRankedValue(
            rank=max(1, int(_number(item.get("rank")) or index)),
            draft_ids=draft_ids,
            claim=_text(item.get("claim")),
            commercial_role=_text(item.get("commercial_role")).lower(),
            retain_reason=_text(item.get("retain_reason")),
            evidence_source_ids=_as_int_tuple(item.get("evidence_source_ids")),
            proof_budget=min(3, max(1, int(_number(item.get("proof_budget")) or 1))),
        ))
    values.sort(key=lambda item: item.rank)
    if not values or len(values) > 5:
        raise ValueError("Commercial Value Ranking 必须保留1-5个购买价值")
    known_drafts = {beat.draft_id for beat in draft.buying_path}
    if any(not item.draft_ids or set(item.draft_ids) - known_drafts for item in values):
        raise ValueError("Commercial Value Ranking 引用了不存在的 Draft 步骤")
    raw_dropped = data.get("dropped_draft_ids") or ()
    if isinstance(raw_dropped, (str, Mapping)):
        raw_dropped = (raw_dropped,)
    dropped_ids: list[str] = []
    dropped_reasons: dict[str, str] = {}
    for item in raw_dropped if isinstance(raw_dropped, Sequence) else ():
        if isinstance(item, Mapping):
            draft_id = _text(item.get("draft_id"))
            reason = _text(item.get("reason"))
        else:
            draft_id = _text(item)
            reason = ""
        if draft_id and draft_id not in dropped_ids:
            dropped_ids.append(draft_id)
            if reason:
                dropped_reasons[draft_id] = reason
    return CommerceLiteCommercialRanking(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        retained_values=tuple(values),
        dropped_draft_ids=tuple(dropped_ids),
        dropped_reasons=dropped_reasons,
        ranking_reason=_text(data.get("ranking_reason")),
    )


def _ranking_grounding_audit(ranking: CommerceLiteCommercialRanking, tags: Sequence[CommerceLiteTag]) -> dict[str, Any]:
    available = {tag.candidate_id for tag in tags if tag.materializable}
    rows = []
    unsupported = []
    for item in ranking.retained_values:
        ids = set(item.evidence_source_ids)
        unknown = sorted(ids - available)
        if not ids or unknown:
            unsupported.append(item.rank)
        rows.append({
            "rank": item.rank,
            "evidence_source_ids": sorted(ids),
            "unknown_source_ids": unknown,
            "grounded": bool(ids) and not unknown,
        })
    return {"grounded": not unsupported, "unsupported_ranks": unsupported, "values": rows}


def _parse_proof_allocations(data: Mapping[str, Any]) -> tuple[CommerceLiteProofAllocation, ...]:
    raw_allocations = data.get("proof_allocations") or ()
    if isinstance(raw_allocations, Mapping):
        raw_allocations = (raw_allocations,)
    allocations: list[CommerceLiteProofAllocation] = []
    for item in raw_allocations if isinstance(raw_allocations, Sequence) else ():
        if not isinstance(item, Mapping):
            continue
        selected: list[CommerceLiteProofDecision] = []
        raw_selected = item.get("selected_proofs") or ()
        if isinstance(raw_selected, Mapping):
            raw_selected = (raw_selected,)
        for proof in raw_selected if isinstance(raw_selected, Sequence) else ():
            if not isinstance(proof, Mapping):
                continue
            candidate_ids = _as_int_tuple(proof.get("candidate_id"))
            if candidate_ids:
                selected.append(CommerceLiteProofDecision(
                    candidate_id=candidate_ids[0], evidence_role=_text(proof.get("evidence_role")).lower(),
                ))
        discarded: dict[int, str] = {}
        raw_discarded = item.get("discarded_proofs") or ()
        if isinstance(raw_discarded, Mapping):
            raw_discarded = (raw_discarded,)
        for proof in raw_discarded if isinstance(raw_discarded, Sequence) else ():
            if not isinstance(proof, Mapping):
                continue
            candidate_ids = _as_int_tuple(proof.get("candidate_id"))
            reason = _text(proof.get("discard_reason")).lower()
            if candidate_ids and reason:
                discarded[candidate_ids[0]] = reason
        allocations.append(CommerceLiteProofAllocation(
            value_rank=max(1, int(_number(item.get("value_rank")) or 1)),
            selected_proofs=tuple(selected),
            discarded_proof_reasons=discarded,
        ))
    return tuple(allocations)


def _minimal_proof_audit(
    *, plan: NarrativePlan, ranking: CommerceLiteCommercialRanking, raw_final: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify model-declared proof budgets without selecting or editing clips."""
    allocations = _parse_proof_allocations(raw_final)
    by_rank = {item.value_rank: item for item in allocations}
    expected_ranks = {item.rank for item in ranking.retained_values}
    actual_selected = {
        proof.candidate_id
        for allocation in allocations
        for proof in allocation.selected_proofs
    }
    plan_selected = {
        candidate_id
        for beat in plan.beats
        for candidate_id in beat.candidate_ids
    }
    errors: list[str] = []
    if set(by_rank) != expected_ranks:
        errors.append("proof_allocation_ranks_mismatch")
    allowed_discard_reasons = {"duplicate_claim", "weaker_proof", "low_purchase_value", "already_proven"}
    ranking_map = {item.rank: item for item in ranking.retained_values}
    rows = []
    for rank, value in ranking_map.items():
        allocation = by_rank.get(rank)
        selected = {proof.candidate_id for proof in allocation.selected_proofs} if allocation else set()
        discarded = set(allocation.discarded_proof_reasons) if allocation else set()
        candidates = set(value.evidence_source_ids)
        if not allocation:
            errors.append(f"proof_allocation_missing:rank={rank}")
        elif len(selected) > value.proof_budget:
            errors.append(f"proof_budget_exceeded:rank={rank},actual={len(selected)},max={value.proof_budget}")
        elif not selected:
            errors.append(f"proof_selection_missing:rank={rank}")
        if candidates and (selected | discarded) != candidates:
            errors.append(f"proof_decision_incomplete:rank={rank}")
        if allocation and any(reason not in allowed_discard_reasons for reason in allocation.discarded_proof_reasons.values()):
            errors.append(f"proof_discard_reason_invalid:rank={rank}")
        rows.append({
            "value_rank": rank,
            "proof_budget": value.proof_budget,
            "selected_candidate_ids": sorted(selected),
            "discarded_candidate_ids": sorted(discarded),
        })
    if actual_selected != plan_selected:
        errors.append("proof_allocation_does_not_match_final_candidates")
    return {
        "passed": not errors,
        "errors": list(dict.fromkeys(errors)),
        "allocations": [item.to_dict() for item in allocations],
        "rows": rows,
        "selected_candidate_ids": sorted(actual_selected),
        "final_candidate_ids": sorted(plan_selected),
        "boundary": "deterministic_audit_only_no_local_proof_selection_or_reorder",
    }


def build_commerce_lite_final_prompt(
    *, strategy: Any, draft: CommerceLiteDraft, tags: Sequence[CommerceLiteTag],
    target_duration: float, selection_contract: Mapping[str, Any] | None,
    ranking: CommerceLiteCommercialRanking | None = None,
) -> str:
    """Turn the non-executable draft into the only executable Lite plan."""
    story_budget = commerce_lite_story_budget(target_duration)
    return "\n".join((
        "你在做 Commercial Planner Lite 的 Final 阶段：把已给出的 Draft 压缩成一条可物化商业片。",
        "Draft 是购买路径地图，不是片单。现在才从完整 hard-safe 候选中选择 candidate_id；只能使用真实候选，不能创作字幕或购买事实。",
        "必须保留 Draft 中能被真实素材支持的关键购买价值，删除重复 claim 和低优先级证明；若 Draft 的某步没有真实候选，诚实省略。",
        "若提供了 Commercial Value Ranking，Final 只能围绕其中 retained_values 的购买价值选择和组织证据；"
        "不得把 dropped_draft_ids 对应的低优先级信息重新带回成片。Ranking 不是候选白名单，仍可从完整候选池选择更完整的同价值表达。",
        "对每个 retained value，必须把 evidence_source_ids 视为可选事实来源而非待消费清单。遵守 proof_budget，选择最小充分证明集："
        "痛点、机制、结果优先；同一 claim 只保留足以让用户相信的少量证据，不能因为来源很多就全用。",
        "必须返回 proof_allocations：每个 value_rank 的 selected_proofs 写 candidate_id 与 evidence_role（pain/mechanism/result/risk_remove/trust），"
        "并为该价值的每一个未选 evidence_source_id 写 discard_reason（duplicate_claim/weaker_proof/low_purchase_value/already_proven）。",
        "proof_allocations 是 Final 的审计合同：所有章节 candidate_ids 必须恰好来自 selected_proofs，且每个 value 不得超过 Ranking 的 proof_budget。",
        "不要只因为 Final 要短就把购买路径剪成单薄主线：在核心痛点→机制→可见结果后，优先保留一个身材包容、一个舒适/信任、",
        "以及一个真实存在的购买风险解除或场景扩展。",
        "这是严格 Final 合同，必须一次提交合法计划：最多7章、总候选数和各桶时长遵守预算。核心至少占50%；辅助章节只回答一个",
        "购买决策问题，舒适/信任/尺码/搭配/耐穿最多1个候选，身材延展最多2个候选。",
        "若有可物化的 M1 Bridge 资产，必须在 Final 中真实选择并在 story_consumption 中准确声明；不要写声明而不消费。",
        "C1 是独立完整 Hook，C2 立即兑现。C1+C2可共享同一 outcome；从C3起每章都必须新增具体购买结果，",
        "除非明确是唯一一次 same_claim_additional_proof。不能以近义词伪装重复。",
        "Final 可比 Draft 少，但不得为了凑目标时长重复同一 claim；素材不够时必须写 insufficient_material。",
        f"目标时长约 {max(1.0, float(target_duration)):.1f}s。",
        "严格预算：",
        json.dumps(story_budget, ensure_ascii=False, separators=(",", ":")),
        "M1 固定商业故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "Draft 购买路径（非可执行、不可原样照抄候选）：",
        json.dumps(draft.to_dict(), ensure_ascii=False, separators=(",", ":")),
        "Commercial Value Ranking（若有，则是 Final 的购买价值取舍边界）：",
        json.dumps(ranking.to_dict() if ranking else {}, ensure_ascii=False, separators=(",", ":")),
        "Selection Contract：",
        json.dumps(dict(selection_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "完整可物化 hard-safe 候选：",
        json.dumps([tag.to_dict() for tag in tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "opening_package": {
                "hook_promise": "", "payoff_delivery": "", "connection_reason": "",
                "hook_candidate_ids": [], "payoff_candidate_ids": [], "hook_integrity_reason": "",
            },
            "chapters": [{
                "chapter_id": "C1", "narrative_role": "hook", "goal": "", "candidate_ids": [],
                "asset_tier": "core/supporting/bridge/safe_reserve", "story_support": "",
                "commerce_beat_id": "identity/difference/problem_or_benefit/visible_result/proof/scene_or_audience/trust",
                "value_dimension": "", "purchase_value_dimension": "new_outcome/same_claim_additional_proof",
                "purchase_value_domain": "", "purchase_value_outcomes": [], "purchase_value_reason": "",
            }],
            "story_consumption": {
                "hero_strategy_id": "", "hero_priority": "", "hero_consistency_reason": "",
                "supporting_chapter_ids": [], "bridge_chapter_ids": [], "no_rediscovery": True,
                "supporting_candidate_ids": [], "bridge_candidate_ids": [],
            },
            "duration_assessment": {"status": "full/insufficient_material", "reason": ""},
            "proof_allocations": [{
                "value_rank": 1,
                "selected_proofs": [{"candidate_id": 0, "evidence_role": "pain/mechanism/result/risk_remove/trust"}],
                "discarded_proofs": [{"candidate_id": 0, "discard_reason": "duplicate_claim/weaker_proof/low_purchase_value/already_proven"}],
            }],
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _post_lite_request(
    *, api_key: str, base_url: str, model: str, prompt: str, stage: str = COMMERCE_PLANNER_LITE_STAGE,
    max_tokens: int = 2600,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只做基于真实候选的商业组合排序；不能发明字幕、候选或购买事实。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "top_p": 0.8,
        # Response capacity only. Individual narrow stages can request more
        # room when their fixed JSON schema is larger than a final M2 plan.
        "max_tokens": max(256, int(max_tokens)),
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body["thinking"] = {"type": "disabled"}
    request = urllib.request.Request(
        ai_chat_completions_url(base_url), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180, context=create_ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        record_ai_call(module="commerce_planner_lite", stage=stage, model=model, request_payload=body, success=False, error_type=f"http_{error.code}")
        raise RuntimeError(f"Commerce Planner Lite HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(module="commerce_planner_lite", stage=stage, model=model, request_payload=body, success=False, error_type=type(error).__name__)
        raise RuntimeError(f"Commerce Planner Lite 网络错误: {error}") from error
    record_ai_call(module="commerce_planner_lite", stage=stage, model=model, request_payload=body, response_payload=result, success=True)
    return result


def _journey_state(plan: NarrativePlan) -> dict[str, bool]:
    """Describe already-covered buyer questions from the submitted Lite plan."""
    domains = {
        _text(beat.purchase_value_domain).lower()
        for beat in plan.beats
        if _text(beat.purchase_value_domain)
    }
    body_chapters = [
        beat for beat in plan.beats
        if _text(beat.purchase_value_domain).lower() in {"body_appearance", "body_shape", "fit", "silhouette"}
    ]
    return {
        "problem": bool(plan.beats and plan.beats[0].narrative_role == "hook"),
        "mechanism": len(plan.beats) >= 2 and plan.beats[1].narrative_role in {"payoff", "payoff_delivery", "difference", "mechanism"},
        "visible_result": len(body_chapters) >= 2,
        "body_fit": any(
            any(value in {"waist_definition", "waist_slimming", "hip_coverage", "hip_slimming", "size_inclusion", "back_thinning"}
                for value in beat.purchase_value_outcomes)
            for beat in body_chapters
        ),
        "comfort": bool(domains.intersection({"comfort", "thermal_comfort"})),
        "trust": bool(domains.intersection({"trust", "safety", "quality"})),
        "fit_risk_reduction": bool(domains.intersection({"size", "size_inclusion", "fit_range", "wearing_security"})),
        "long_term_risk_reduction": bool(domains.intersection({"durability", "care"})),
        "scene": bool(domains.intersection({"scene", "styling", "lifestyle"})),
    }


def _completion_missing_journeys(state: Mapping[str, bool]) -> tuple[str, ...]:
    """Prioritize friction-reduction before optional trust or scene expansion."""
    priority = (
        "fit_risk_reduction",
        "long_term_risk_reduction",
        "trust",
        "scene",
    )
    return tuple(key for key in priority if not bool(state.get(key)))


def _completion_tag_payload(tags: Sequence[CommerceLiteTag], selected_ids: set[int]) -> list[dict[str, Any]]:
    """Keep every unused legal candidate visible; this is not a local shortlist."""
    return [
        {
            "candidate_id": tag.candidate_id,
            "text": tag.text,
            "duration": round(float(tag.duration), 3),
            "asset_role": tag.asset_role,
            "story_permission": tag.story_permission,
            "m1_tiers": list(tag.m1_tiers),
            "m1_roles": list(tag.m1_roles),
            "m1_claims": list(tag.m1_claims),
            "purchase_value_hints": list(tag.purchase_value_hints),
            "materializable": tag.materializable,
        }
        for tag in tags
        if tag.candidate_id not in selected_ids
    ]


def build_commerce_lite_completion_prompt(
    *, strategy: Any, current_plan: NarrativePlan, tags: Sequence[CommerceLiteTag], target_duration: float,
) -> str:
    """Ask for bounded additions only; existing Lite chapters are immutable."""
    budget = commerce_lite_story_budget(target_duration)
    current = [
        {
            "chapter_id": beat.chapter_id,
            "candidate_ids": list(beat.candidate_ids),
            "role": beat.narrative_role,
            "purchase_value_domain": beat.purchase_value_domain,
            "purchase_value_outcomes": list(beat.purchase_value_outcomes),
            "purchase_value_reason": beat.purchase_value_reason,
        }
        for beat in current_plan.beats
    ]
    state = _journey_state(current_plan)
    missing = _completion_missing_journeys(state)
    chapter_capacity = max(0, int(budget["maximum_chapters"]) - len(current_plan.beats))
    return "\n".join((
        "你是 Commercial Planner Lite 的 Completion Pass。你的职责不是重新选片，也不是重写故事。",
        "当前 Lite 计划、Opening 和章节顺序已经冻结：不得替换、删除、改写或重排已有章节；只能在最后追加 1-3 个新章节。",
        "只有在当前计划偏短且购买路径缺关键环节时才补。优先补购买风险解除（尺码/体型包容/穿着安全/耐穿），其次才是信任或场景。",
        "每个新增章节必须引入一个未覆盖的购买结果；不得复述显瘦、面料、尺码或任何已覆盖 outcome。",
        "辅助章节默认最多一个候选，不能用多句近义证明凑时长。所有新增章节合计不超过25秒，最终总章数不能超过预算。",
        "若剩余素材无法在这些条件下补出新的购买价值，返回 no_materializable_completion；这是正确结果。",
        f"当前真实时长 {current_plan.total_seconds:.3f}s；目标 {float(target_duration):.1f}s。",
        "固定 M1 故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前计划（不可修改）：",
        json.dumps(current, ensure_ascii=False, separators=(",", ":")),
        "购买路径覆盖：",
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        "优先补足：",
        json.dumps(list(missing), ensure_ascii=False, separators=(",", ":")),
        "预算：",
        json.dumps({
            "maximum_new_chapters": min(3, chapter_capacity),
            "maximum_new_seconds": 25.0,
            "final_story_budget": budget,
        }, ensure_ascii=False, separators=(",", ":")),
        "全部尚未使用的可物化 hard-safe 候选（无本地筛选）：",
        json.dumps(_completion_tag_payload(tags, {candidate.candidate_id for candidate in current_plan.selected_candidates}), ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "status": "completed/no_materializable_completion",
            "reason": "",
            "append_chapters": [{
                "chapter_id": f"C{len(current_plan.beats) + 1}", "narrative_role": "risk_reduction/trust/scene", "goal": "",
                "candidate_ids": [], "asset_tier": "supporting/bridge/safe_reserve", "story_support": "",
                "commerce_beat_id": "trust/scene_or_audience/proof",
                "value_dimension": "", "purchase_value_dimension": "new_outcome",
                "purchase_value_domain": "", "purchase_value_outcomes": [], "purchase_value_reason": "",
            }],
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _completion_audit(
    *, current_plan: NarrativePlan, tags: Sequence[CommerceLiteTag], target_duration: float,
) -> dict[str, Any]:
    state = _journey_state(current_plan)
    return {
        "stage": "commercial_completion_pass_v1",
        "initial_seconds": current_plan.total_seconds,
        # A 45-second commercial story is the first useful completion floor
        # for these experiments. A user asking for a shorter target must not
        # trigger extra model work merely because the story is below 45s.
        "trigger_threshold_seconds": round(min(45.0, max(1.0, float(target_duration))), 3),
        "purchase_journey": state,
        "missing_journeys": list(_completion_missing_journeys(state)),
        "unused_materializable_candidate_count": sum(
            1 for tag in tags if tag.materializable and tag.candidate_id not in {candidate.candidate_id for candidate in current_plan.selected_candidates}
        ),
    }


def build_commerce_lite_chapter_compression_prompt(
    *, strategy: Any, current_plan: NarrativePlan, tags: Sequence[CommerceLiteTag], target_duration: float,
) -> str:
    """Ask M2.4 to stop completed chapters, then fill only real journey gaps."""
    frozen = [{
        "chapter_id": beat.chapter_id,
        "narrative_role": beat.narrative_role,
        "goal": beat.goal,
        "purchase_value_domain": beat.purchase_value_domain,
        "purchase_value_outcomes": list(beat.purchase_value_outcomes),
        "candidate_ids": list(beat.candidate_ids),
    } for beat in current_plan.beats]
    return "\n".join((
        "你在做 M2.4 Chapter Compression Pass。输入是一份 M2 初稿，不是让你发现新故事、重做 Ranking 或重排故事。",
        "固定 M1 主线、前置章节顺序和每章的购买决策问题。你的工作是让每个章节在证明已经充分时停止，",
        "删掉同一机制/同一结果的重复证明；随后只为缺失的、不同购买决策问题补 1-2 个章节。",
        "不能改写字幕、不能制造事实、不能使用不可物化候选、不能把价格/福利/其他商品当补时长。",
        "C1+C2 是 Opening Unit：必须共同完成痛点/承诺到立即兑现。不要把直播聊天语气当成独立 Hook；",
        "但也不能改写候选文本。若没有更好的完整 Opening，诚实保留风险说明。",
        "原有章节必须按相同 chapter_id 和 narrative_role 原顺序保留；可减少或替换每章内候选以达到完成条件。",
        "只可在原章节之后追加最多 2 个新章节，且每个追加章节必须是一个新的购买决策问题（如穿着安全、",
        "尺寸风险、舒适、信任或场景），不能补同一显瘦 claim。",
        "Bridge 不是必须塞进 Opening。只有选用了真实 Bridge 资产，才在 story_consumption 中声明它；",
        "若 M1 的 Bridge 只适合舒适或场景，就在其自然的后置位置使用，不得虚构“设计承接句”。",
        "章节完成条件（达到即停止）：pain/hook 最多2段；mechanism 最多2段且最多7秒；",
        "visible_result/payoff 最多2段且最多7秒；body_extension 最多2段；comfort/trust/scene/risk_reduction 最多1段。",
        "压缩后若偏短，优先补缺失购买阻力解除或场景价值；不许为了达到目标重复已有证明。",
        f"目标时长约 {max(1.0, float(target_duration)):.1f}s。",
        "章节饱和审计合同：",
        json.dumps(commerce_lite_chapter_saturation_budget(target_duration), ensure_ascii=False, separators=(",", ":")),
        "M1 固定商业故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "待压缩初稿（其候选和时长可能违法，不能原样照抄）：",
        json.dumps(frozen, ensure_ascii=False, separators=(",", ":")),
        "完整候选商业标签：",
        json.dumps([tag.to_dict() for tag in tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "opening_package": {"hook_promise": "", "payoff_delivery": "", "connection_reason": "", "hook_candidate_ids": [], "payoff_candidate_ids": [], "hook_integrity_reason": ""},
            "chapters": [{"chapter_id": "C1", "narrative_role": "hook", "goal": "", "candidate_ids": [], "asset_tier": "core/supporting/bridge/safe_reserve", "story_support": "", "commerce_beat_id": "", "value_dimension": "", "purchase_value_dimension": "new_outcome/same_claim_additional_proof", "purchase_value_domain": "", "purchase_value_outcomes": [], "purchase_value_reason": ""}],
            "story_consumption": {"hero_strategy_id": "", "hero_priority": "", "hero_consistency_reason": "", "supporting_chapter_ids": [], "bridge_chapter_ids": [], "no_rediscovery": True, "supporting_candidate_ids": [], "bridge_candidate_ids": []},
            "duration_assessment": {"status": "full/insufficient_material", "reason": ""},
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _editor_chapter_payload(
    plan: NarrativePlan,
    tags: Sequence[CommerceLiteTag],
) -> list[dict[str, Any]]:
    """Expose measured plan facts to an editor without granting timing authority."""
    duration_by_id = {tag.candidate_id: float(tag.duration) for tag in tags}
    return [{
        "chapter_id": beat.chapter_id,
        "narrative_role": beat.narrative_role,
        "goal": beat.goal,
        "candidate_ids": list(beat.candidate_ids),
        "actual_seconds": round(sum(duration_by_id.get(candidate_id, 0.0) for candidate_id in beat.candidate_ids), 3),
        "purchase_value_domain": beat.purchase_value_domain,
        "purchase_value_outcomes": list(beat.purchase_value_outcomes),
        "purchase_value_reason": beat.purchase_value_reason,
    } for beat in plan.beats]


def _editor_measured_seconds(plan: NarrativePlan, tags: Sequence[CommerceLiteTag]) -> float:
    """Recompute source time from frozen candidate IDs, never from model claims."""
    duration_by_id = {tag.candidate_id: float(tag.duration) for tag in tags}
    return round(sum(
        duration_by_id.get(candidate_id, 0.0)
        for beat in plan.beats for candidate_id in beat.candidate_ids
    ), 3)


def build_commerce_lite_final_editor_prompt(
    *, strategy: Any, current_plan: NarrativePlan, tags: Sequence[CommerceLiteTag], target_duration: float,
) -> str:
    """Ask for bounded edit operations, never another NarrativePlan.

    The model judges editorial trade-offs only.  Candidate duration, chapter
    IDs, final plan construction and every materialization boundary stay
    deterministic in the caller.
    """
    current = _editor_chapter_payload(current_plan, tags)
    existing_outcomes = sorted({
        outcome for item in current for outcome in item["purchase_value_outcomes"] if outcome
    })
    return "\n".join((
        "你是 Commercial Final Editor。你不是重新导演、重新排序或自我解释原计划。",
        "你只能提交明确的编辑动作，程序会以真实候选时长、候选血缘、章节上限和购买价值去执行并审计。",
        "不要报告或估算总时长；不要输出完整 Plan；不要改写字幕、不能创造候选或购买事实。",
        "先删除同一章节中较弱的重复证明；再只在购买路径缺少新决策问题时追加 1-3 个章节。",
        "追加必须是新的购买结果（如穿着安全、尺码风险、信任、场景），不是再次证明显瘦或凉爽。",
        "原章节顺序和角色冻结；不能删空原章节，不能在原章节中加入候选，不能改 Opening 文字。",
        "若没有安全且有价值的编辑，返回 no_safe_edit；这比编造动作正确。",
        "你不得依据、猜测或讨论最终时长来拒绝一个新增购买价值。时长、章节预算和是否仍然偏短完全由程序计算；",
        "你的唯一判断是：这条真实候选是否提供了尚未覆盖的消费者购买理由，或已有候选是否只是较弱重复证明。",
        "若返回 no_safe_edit，considered_candidate_ids 最多列 12 个有代表性的已审阅候选；",
        "rejected_candidates 最多列 8 个代表性拒绝样本。绝不能逐条罗列完整候选池。",
        "固定 M1 商业故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前计划（actual_seconds 是程序测得，唯一有效）：",
        json.dumps(current, ensure_ascii=False, separators=(",", ":")),
        "当前已覆盖 outcomes（追加不得重复）：",
        json.dumps(existing_outcomes, ensure_ascii=False, separators=(",", ":")),
        "全部可物化 hard-safe 候选（只读，不是本地白名单）：",
        json.dumps([tag.to_dict() for tag in tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "status": "edited/no_safe_edit",
            "reason": "",
            "considered_candidate_ids": [0],
            "rejected_candidates": [{"candidate_id": 0, "reason": "duplicate_claim/not_new_purchase_value/not_self_contained"}],
            "operations": [
                {"op": "remove_candidate", "chapter_id": "C2", "candidate_id": 0, "reason": "duplicate_or_weaker_proof"},
                {
                    "op": "append_chapter", "narrative_role": "risk_reduction/trust/scene/body_extension/comfort",
                    "goal": "", "candidate_ids": [0], "asset_tier": "supporting/bridge/safe_reserve",
                    "story_support": "", "commerce_beat_id": "", "value_dimension": "",
                    "purchase_value_dimension": "new_outcome", "purchase_value_domain": "",
                    "purchase_value_outcomes": [""], "purchase_value_reason": "",
                    "reason": "missing_purchase_journey",
                },
            ],
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _apply_final_editor_operations(
    *, current_plan: NarrativePlan, data: Mapping[str, Any], tags: Sequence[CommerceLiteTag],
) -> tuple[tuple[Any, ...] | None, dict[str, Any]]:
    """Apply a bounded action list without selecting or repairing locally."""
    raw_operations = data.get("operations") or ()
    if isinstance(raw_operations, Mapping):
        raw_operations = (raw_operations,)
    if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, (str, bytes, bytearray)):
        raw_operations = ()
    chapters = [beat.to_dict() for beat in current_plan.beats]
    chapter_by_id = {str(item.get("chapter_id") or ""): item for item in chapters}
    original_ids = {candidate_id for beat in current_plan.beats for candidate_id in beat.candidate_ids}
    available_ids = {tag.candidate_id for tag in tags if tag.materializable}
    covered_outcomes = {
        outcome for beat in current_plan.beats for outcome in beat.purchase_value_outcomes if outcome
    }
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    append_count = 0
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            rejected.append({"reason": "operation_not_object"})
            continue
        op = _text(raw.get("op")).lower()
        if op == "remove_candidate":
            chapter_id = _text(raw.get("chapter_id"))
            candidate_ids = _as_int_tuple(raw.get("candidate_id"))
            candidate_id = candidate_ids[0] if candidate_ids else 0
            chapter = chapter_by_id.get(chapter_id)
            if chapter is None or candidate_id not in _as_int_tuple(chapter.get("candidate_ids")):
                rejected.append({"op": op, "chapter_id": chapter_id, "candidate_id": candidate_id, "reason": "not_selected_in_named_chapter"})
                continue
            remaining = [item for item in _as_int_tuple(chapter.get("candidate_ids")) if item != candidate_id]
            if not remaining:
                rejected.append({"op": op, "chapter_id": chapter_id, "candidate_id": candidate_id, "reason": "would_empty_frozen_chapter"})
                continue
            chapter["candidate_ids"] = remaining
            chapter["candidate_evidence"] = remaining
            original_ids.discard(candidate_id)
            applied.append({"op": op, "chapter_id": chapter_id, "candidate_id": candidate_id, "reason": _text(raw.get("reason"))})
            continue
        if op == "append_chapter":
            if append_count >= 3:
                rejected.append({"op": op, "reason": "append_limit_exceeded"})
                continue
            candidate_ids = _as_int_tuple(raw.get("candidate_ids"))
            outcomes = _as_text_tuple(raw.get("purchase_value_outcomes"))
            role = _text(raw.get("narrative_role")).lower()
            if not candidate_ids or not outcomes or not role:
                rejected.append({"op": op, "reason": "missing_required_append_fields"})
                continue
            if any(candidate_id not in available_ids for candidate_id in candidate_ids):
                rejected.append({"op": op, "candidate_ids": list(candidate_ids), "reason": "candidate_not_materializable"})
                continue
            if any(candidate_id in original_ids for candidate_id in candidate_ids):
                rejected.append({"op": op, "candidate_ids": list(candidate_ids), "reason": "candidate_already_selected"})
                continue
            if set(outcomes).issubset(covered_outcomes):
                rejected.append({"op": op, "outcomes": list(outcomes), "reason": "not_a_new_purchase_outcome"})
                continue
            append_count += 1
            chapter_id = f"C{len(chapters) + 1}"
            chapter = {
                "chapter_id": chapter_id,
                "source_role": "",
                "narrative_role": role,
                "goal": _text(raw.get("goal")),
                "candidate_ids": list(candidate_ids),
                "candidate_evidence": list(candidate_ids),
                "required": True,
                "target_seconds": 0.0,
                "selection_instruction": "",
                "asset_tier": _text(raw.get("asset_tier")) or "supporting",
                "selection_origin": "final_editor_v1",
                "transition_from_previous": "",
                "value_dimension": _text(raw.get("value_dimension")),
                "purchase_value_dimension": _text(raw.get("purchase_value_dimension")) or "new_outcome",
                "purchase_value_domain": _text(raw.get("purchase_value_domain")),
                "purchase_value_outcomes": list(outcomes),
                "purchase_value_reason": _text(raw.get("purchase_value_reason")),
                "story_support": _text(raw.get("story_support")),
                "commerce_beat_id": _text(raw.get("commerce_beat_id")),
            }
            chapters.append(chapter)
            chapter_by_id[chapter_id] = chapter
            original_ids.update(candidate_ids)
            covered_outcomes.update(outcomes)
            applied.append({"op": op, "chapter_id": chapter_id, "candidate_ids": list(candidate_ids), "outcomes": list(outcomes), "reason": _text(raw.get("reason"))})
            continue
        rejected.append({"op": op or "unknown", "reason": "unsupported_operation"})
    if not applied:
        considered = _as_int_tuple(data.get("considered_candidate_ids"))[:12]
        rejected_candidates = data.get("rejected_candidates") or ()
        if isinstance(rejected_candidates, Mapping):
            rejected_candidates = (rejected_candidates,)
        return None, {
            "status": "no_operations_applied" if considered else "unsubstantiated_no_safe_edit",
            "model_status": _text(data.get("status")).lower(),
            "model_reason": _text(data.get("reason")),
            "considered_candidate_ids": list(considered),
            "model_rejected_candidates": [
                dict(item) for item in rejected_candidates if isinstance(item, Mapping)
            ][:8],
            "applied_operations": applied,
            "rejected_operations": rejected,
        }
    return _parse_beats({"chapters": chapters}), {
        "status": "operations_applied",
        "model_status": _text(data.get("status")).lower(),
        "model_reason": _text(data.get("reason")),
        "applied_operations": applied,
        "rejected_operations": rejected,
    }


def plan_commerce_lite_final_editor_llm(
    *, strategy: Any, current_plan: NarrativePlan, tags: Sequence[CommerceLiteTag], target_duration: float,
    safe_candidates: Sequence[PlanningCandidate], selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]], api_key: str, base_url: str, model: str,
    response_hook: Callable[[str], None] | None = None,
) -> NarrativePlan:
    """Run one editorial action pass; program owns plan assembly and duration."""
    prompt = build_commerce_lite_final_editor_prompt(
        strategy=strategy, current_plan=current_plan, tags=tags, target_duration=target_duration,
    )
    result = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model, prompt=prompt,
        stage=COMMERCE_LITE_FINAL_EDITOR_STAGE,
    )
    content = _text(result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not content:
        raise RuntimeError("Final Editor 返回空内容")
    if response_hook:
        response_hook(content)
    data = _extract_json(content)
    beats, audit = _apply_final_editor_operations(current_plan=current_plan, data=data, tags=tags)
    audit.update({
        "stage": "final_editor_v1",
        "source_seconds": _editor_measured_seconds(current_plan, tags),
        "source_chapter_ids": [beat.chapter_id for beat in current_plan.beats],
        "program_measures_duration": True,
    })
    if beats is None:
        assessment = dict(current_plan.duration_assessment or {})
        audit["final_seconds"] = _editor_measured_seconds(current_plan, tags)
        audit["final_plan_valid"] = current_plan.plan_valid
        assessment["commerce_lite_final_editor"] = audit
        return replace(current_plan, duration_assessment=assessment)
    contract = dict(selection_contract or current_plan.selection_contract or {})
    contract.update({
        "commerce_lite_purchase_value_progression": True,
        "commerce_lite_story_budget": True,
        "commerce_lite_chapter_saturation": True,
        "commerce_lite_final_editor_experiment": True,
    })
    edited = replace(
        current_plan,
        beats=beats,
        selected_candidates=(),
        replan_request=None,
        issues=(),
        plan_valid=True,
        selection_contract=contract,
        duration_assessment={},
    )
    annotated = bind_story_assets(CommercialStoryBrief.from_strategy(strategy), tuple(safe_candidates))
    validated = validate_narrative_plan(edited, annotated, executable_evidence=executable_evidence)
    assessment = dict(validated.duration_assessment or {})
    audit["final_seconds"] = validated.total_seconds
    audit["final_plan_valid"] = validated.plan_valid
    audit["final_chapter_ids"] = [beat.chapter_id for beat in validated.beats]
    assessment["commerce_lite_final_editor"] = audit
    return replace(validated, duration_assessment=assessment)


def plan_commerce_lite_chapter_compression_llm(
    *,
    strategy: Any,
    current_plan: NarrativePlan,
    tags: Sequence[CommerceLiteTag],
    target_duration: float,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str], None] | None = None,
) -> NarrativePlan:
    """Offline M2.4 compression/completion experiment; M3 remains unchanged."""
    if not current_plan.beats:
        raise ValueError("Chapter Compression requires an M2 draft plan")
    prompt = build_commerce_lite_chapter_compression_prompt(
        strategy=strategy, current_plan=current_plan, tags=tags, target_duration=target_duration,
    )
    result = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model, prompt=prompt,
        stage=COMMERCE_LITE_CHAPTER_COMPRESSION_STAGE,
    )
    content = _text(result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not content:
        raise RuntimeError("Chapter Compression Pass 返回空内容")
    if response_hook:
        response_hook(content)
    data = _extract_json(content)
    contract = dict(selection_contract or {})
    contract.update({
        "commerce_lite_purchase_value_progression": True,
        "commerce_lite_story_budget": True,
        "commerce_lite_chapter_saturation": True,
        "commerce_lite_chapter_compression_experiment": True,
    })
    plan = NarrativePlan(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        thesis=_text(getattr(strategy, "thesis", "")),
        target_duration=float(target_duration),
        beats=_parse_beats(data),
        status="insufficient_material",
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=contract,
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=float(target_duration)),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=float(target_duration)),
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    original = [(beat.chapter_id, beat.narrative_role) for beat in current_plan.beats]
    actual = [(beat.chapter_id, beat.narrative_role) for beat in plan.beats]
    structure_issues: list[str] = []
    if actual[:len(original)] != original:
        structure_issues.append("chapter_compression_original_structure_changed")
    if len(actual) > len(original) + 2:
        structure_issues.append("chapter_compression_append_limit_exceeded")
    annotated = bind_story_assets(CommercialStoryBrief.from_strategy(strategy), tuple(safe_candidates))
    validated = validate_narrative_plan(plan, annotated, executable_evidence=executable_evidence)
    assessment = dict(validated.duration_assessment or {})
    assessment["commerce_lite_chapter_compression"] = {
        "stage": "chapter_compression_pass_v1",
        "source_plan_seconds": current_plan.total_seconds,
        "source_chapter_ids": [chapter_id for chapter_id, _ in original],
        "returned_chapter_ids": [chapter_id for chapter_id, _ in actual],
        "structure_issues": structure_issues,
        "final_seconds": validated.total_seconds,
        "final_plan_valid_before_structure_audit": validated.plan_valid,
    }
    if structure_issues:
        return replace(
            validated,
            plan_valid=False,
            issues=tuple(list(validated.issues) + structure_issues),
            duration_assessment=assessment,
        )
    return replace(validated, duration_assessment=assessment)


def plan_commerce_lite_llm(
    *,
    strategy: Any,
    tags: Sequence[CommerceLiteTag],
    target_duration: float,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    raw_response_hook: Callable[[str], None] | None = None,
    completion_response_hook: Callable[[str], None] | None = None,
    completion_enabled: bool = True,
) -> NarrativePlan:
    """Rank a Lite plan, then optionally append bounded missing buyer value."""
    if not safe_candidates:
        raise ValueError("Commerce Planner Lite requires the complete materializable hard-safe pool")
    prompt = build_commerce_lite_ranking_prompt(
        strategy=strategy, tags=tags, target_duration=target_duration, selection_contract=selection_contract,
    )
    result = _post_lite_request(api_key=api_key, base_url=base_url, model=model, prompt=prompt)
    content = _text(result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not content:
        raise RuntimeError("Commerce Planner Lite 返回空内容")
    if raw_response_hook:
        raw_response_hook(content)
    data = _extract_json(content)
    lite_contract = dict(selection_contract or {})
    # This flag is a validation instruction for Lite output only.  It neither
    # filters the candidate pool nor grants a local component any reorder or
    # rewrite authority.
    lite_contract["commerce_lite_purchase_value_progression"] = True
    lite_contract["commerce_lite_story_budget"] = True
    plan = NarrativePlan(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        thesis=_text(getattr(strategy, "thesis", "")),
        target_duration=float(target_duration),
        beats=_parse_beats(data),
        status="insufficient_material",
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=lite_contract,
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=float(target_duration)),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=float(target_duration)),
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    # M2's common validator reasons about Core/Supporting/Bridge through the
    # same read-only annotation adapter as the normal planner.  Skipping this
    # step would make a correctly selected Bridge look untyped and falsely
    # fail ``bridge_not_consumed``; it never removes or ranks the pool.
    annotated_candidates = bind_story_assets(
        CommercialStoryBrief.from_strategy(strategy), tuple(safe_candidates),
    )
    validated_initial = validate_narrative_plan(plan, annotated_candidates, executable_evidence=executable_evidence)
    completion = _completion_audit(
        current_plan=validated_initial, tags=tags, target_duration=target_duration,
    )
    if not completion_enabled:
        completion["status"] = "not_used_final_editor_experiment"
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)
    trigger_threshold = float(completion["trigger_threshold_seconds"])
    chapter_capacity = max(0, int(commerce_lite_story_budget(target_duration)["maximum_chapters"]) - len(validated_initial.beats))
    should_attempt = (
        validated_initial.plan_valid
        and validated_initial.total_seconds < trigger_threshold
        and bool(completion["missing_journeys"])
        and chapter_capacity > 0
    )
    if not should_attempt:
        completion["status"] = (
            "not_needed" if validated_initial.total_seconds >= trigger_threshold
            else "not_attempted_initial_plan_invalid" if not validated_initial.plan_valid
            else "no_missing_purchase_journey" if not completion["missing_journeys"]
            else "no_chapter_capacity"
        )
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)

    completion["status"] = "attempted"
    completion_prompt = build_commerce_lite_completion_prompt(
        strategy=strategy, current_plan=validated_initial, tags=tags, target_duration=target_duration,
    )
    try:
        completion_result = _post_lite_request(
            api_key=api_key,
            base_url=base_url,
            model=model,
            prompt=completion_prompt,
            stage=COMMERCE_LITE_COMPLETION_STAGE,
        )
        completion_content = _text(completion_result.get("choices", [{}])[0].get("message", {}).get("content"))
        if not completion_content:
            raise RuntimeError("Completion Pass 返回空内容")
        if completion_response_hook:
            completion_response_hook(completion_content)
        completion_data = _extract_json(completion_content)
    except (RuntimeError, ValueError, TypeError) as error:
        completion["status"] = "completion_call_failed"
        completion["reason"] = str(error)
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)

    completion["model_status"] = _text(completion_data.get("status")).lower()
    completion["reason"] = _text(completion_data.get("reason"))
    raw_append = completion_data.get("append_chapters") or ()
    if isinstance(raw_append, Mapping):
        raw_append = (raw_append,)
    append_beats = _parse_beats({"chapters": raw_append})
    completion["append_chapter_ids"] = [beat.chapter_id for beat in append_beats]
    completion["append_candidate_ids"] = [
        candidate_id for beat in append_beats for candidate_id in beat.candidate_ids
    ]
    if completion["model_status"] != "completed" or not append_beats:
        completion["status"] = "no_materializable_completion"
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)
    if len(append_beats) > min(3, chapter_capacity):
        completion["status"] = "completion_contract_exceeded"
        completion["reason"] = "completion returned too many chapters"
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)
    existing_chapter_ids = {beat.chapter_id for beat in validated_initial.beats if beat.chapter_id}
    duplicate_chapter_ids = sorted({
        beat.chapter_id for beat in append_beats
        if beat.chapter_id and beat.chapter_id in existing_chapter_ids
    })
    if duplicate_chapter_ids:
        # Do not repair a bad append declaration locally: keep the original
        # valid plan intact and report the illegal patch for audit.
        completion["status"] = "completion_contract_exceeded"
        completion["reason"] = f"append chapter IDs already exist: {duplicate_chapter_ids}"
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)

    selected_map = {candidate.candidate_id: candidate for candidate in annotated_candidates}
    append_seconds = round(sum(
        selected_map[candidate_id].duration
        for beat in append_beats
        for candidate_id in beat.candidate_ids
        if candidate_id in selected_map
    ), 3)
    if append_seconds > 25.0:
        completion["status"] = "completion_contract_exceeded"
        completion["reason"] = f"completion seconds {append_seconds:.3f} exceed 25.000"
        assessment = dict(validated_initial.duration_assessment or {})
        assessment["commerce_lite_completion"] = completion
        return replace(validated_initial, duration_assessment=assessment)

    merged_contract = dict(validated_initial.selection_contract or {})
    merged_contract["commerce_lite_completion_pass"] = True
    merged = replace(
        validated_initial,
        beats=tuple(validated_initial.beats) + tuple(append_beats),
        selection_contract=merged_contract,
        selected_candidates=(),
        replan_request=None,
        issues=(),
        plan_valid=True,
        duration_assessment={},
    )
    completed = validate_narrative_plan(merged, annotated_candidates, executable_evidence=executable_evidence)
    completion["status"] = "completed" if completed.plan_valid else "completion_plan_invalid"
    completion["added_seconds"] = append_seconds
    completion["final_seconds"] = completed.total_seconds
    completion["final_plan_valid"] = completed.plan_valid
    if completed.replan_request:
        completion["validation_codes"] = list(completed.replan_request.reason_codes)
    assessment = dict(completed.duration_assessment or {})
    assessment["commerce_lite_completion"] = completion
    return replace(completed, duration_assessment=assessment)


def plan_commerce_lite_draft_final_llm(
    *,
    strategy: Any,
    tags: Sequence[CommerceLiteTag],
    target_duration: float,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    draft_response_hook: Callable[[str], None] | None = None,
    final_response_hook: Callable[[str], None] | None = None,
) -> tuple[CommerceLiteDraft, NarrativePlan]:
    """Offline Draft -> Final experiment; only Final is validated or executable.

    This function intentionally has no replan loop and no local pruning.  The
    first model call makes a candidate-free commercial map; the second call is
    solely responsible for selecting a final, strict-contract plan from the
    same complete candidate pool.  Any illegal Final stays blocked for audit.
    """
    if not safe_candidates:
        raise ValueError("Commerce Planner Lite Draft requires the complete materializable hard-safe pool")
    draft_result = _post_lite_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        prompt=build_commerce_lite_draft_prompt(
            strategy=strategy, tags=tags, target_duration=target_duration,
        ),
        stage=COMMERCE_LITE_DRAFT_STAGE,
    )
    draft_content = _text(draft_result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not draft_content:
        raise RuntimeError("Commerce Planner Lite Draft 返回空内容")
    if draft_response_hook:
        draft_response_hook(draft_content)
    draft = _parse_commerce_lite_draft(
        _extract_json(draft_content), strategy=strategy, target_duration=target_duration,
    )
    draft_grounding = _draft_grounding_audit(draft, tags)

    final_result = _post_lite_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        prompt=build_commerce_lite_final_prompt(
            strategy=strategy,
            draft=draft,
            tags=tags,
            target_duration=target_duration,
            selection_contract=selection_contract,
        ),
        stage=COMMERCE_LITE_DRAFT_FINAL_STAGE,
    )
    final_content = _text(final_result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not final_content:
        raise RuntimeError("Commerce Planner Lite Draft Final 返回空内容")
    if final_response_hook:
        final_response_hook(final_content)
    data = _extract_json(final_content)
    final_contract = dict(selection_contract or {})
    final_contract["commerce_lite_purchase_value_progression"] = True
    final_contract["commerce_lite_story_budget"] = True
    final_contract["commerce_lite_draft_final_experiment"] = True
    plan = NarrativePlan(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        thesis=_text(getattr(strategy, "thesis", "")),
        target_duration=float(target_duration),
        beats=_parse_beats(data),
        status="insufficient_material",
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=final_contract,
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=float(target_duration)),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=float(target_duration)),
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    annotated_candidates = bind_story_assets(
        CommercialStoryBrief.from_strategy(strategy), tuple(safe_candidates),
    )
    validated = validate_narrative_plan(plan, annotated_candidates, executable_evidence=executable_evidence)
    draft_outcomes = {
        outcome
        for beat in draft.buying_path
        for outcome in beat.purchase_value_outcomes
    }
    final_outcomes = {
        outcome
        for beat in validated.beats
        for outcome in beat.purchase_value_outcomes
    }
    assessment = dict(validated.duration_assessment or {})
    assessment["commerce_lite_draft_final"] = {
        "stage": "draft_to_final_v1",
        "draft_step_count": len(draft.buying_path),
        "draft_suggested_duration": draft.suggested_duration,
        "final_chapter_count": len(validated.beats),
        "draft_outcomes": sorted(draft_outcomes),
        "final_outcomes": sorted(final_outcomes),
        "draft_outcomes_not_materialized": sorted(draft_outcomes - final_outcomes),
        "draft_grounding": draft_grounding,
        "final_plan_valid": validated.plan_valid,
        "boundary": "draft_is_candidate_free_final_is_only_m3_input",
    }
    return draft, replace(validated, duration_assessment=assessment)


def plan_commerce_lite_draft_rank_final_llm(
    *,
    strategy: Any,
    tags: Sequence[CommerceLiteTag],
    target_duration: float,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    draft_response_hook: Callable[[str], None] | None = None,
    ranking_response_hook: Callable[[str], None] | None = None,
    final_response_hook: Callable[[str], None] | None = None,
) -> tuple[CommerceLiteDraft, CommerceLiteCommercialRanking, NarrativePlan]:
    """Offline Draft -> commercial ranking -> strict Final experiment only."""
    if not safe_candidates:
        raise ValueError("Commerce Lite Draft Ranking requires the complete materializable hard-safe pool")

    def call(*, prompt: str, stage: str, hook: Callable[[str], None] | None) -> Mapping[str, Any]:
        result = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model, prompt=prompt, stage=stage,
        )
        content = _text(result.get("choices", [{}])[0].get("message", {}).get("content"))
        if not content:
            raise RuntimeError(f"{stage} 返回空内容")
        if hook:
            hook(content)
        return _extract_json(content)

    draft = _parse_commerce_lite_draft(
        call(
            prompt=build_commerce_lite_draft_prompt(strategy=strategy, tags=tags, target_duration=target_duration),
            stage=COMMERCE_LITE_DRAFT_STAGE,
            hook=draft_response_hook,
        ),
        strategy=strategy,
        target_duration=target_duration,
    )
    draft_grounding = _draft_grounding_audit(draft, tags)
    ranking = _parse_commerce_lite_ranking(
        call(
            prompt=build_commerce_lite_commercial_ranking_prompt(strategy=strategy, draft=draft, tags=tags),
            stage=COMMERCE_LITE_RANKING_STAGE,
            hook=ranking_response_hook,
        ),
        strategy=strategy,
        draft=draft,
    )
    ranking_grounding = _ranking_grounding_audit(ranking, tags)
    data = call(
        prompt=build_commerce_lite_final_prompt(
            strategy=strategy,
            draft=draft,
            ranking=ranking,
            tags=tags,
            target_duration=target_duration,
            selection_contract=selection_contract,
        ),
        stage=COMMERCE_LITE_DRAFT_FINAL_STAGE,
        hook=final_response_hook,
    )
    final_contract = dict(selection_contract or {})
    final_contract.update({
        "commerce_lite_purchase_value_progression": True,
        "commerce_lite_story_budget": True,
        "commerce_lite_draft_rank_final_experiment": True,
    })
    plan = NarrativePlan(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        thesis=_text(getattr(strategy, "thesis", "")),
        target_duration=float(target_duration),
        beats=_parse_beats(data),
        status="insufficient_material",
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=final_contract,
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=float(target_duration)),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=float(target_duration)),
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    annotated_candidates = bind_story_assets(CommercialStoryBrief.from_strategy(strategy), tuple(safe_candidates))
    validated = validate_narrative_plan(plan, annotated_candidates, executable_evidence=executable_evidence)
    proof_audit = _minimal_proof_audit(plan=validated, ranking=ranking, raw_final=data)
    if not proof_audit["passed"]:
        existing_replan = validated.replan_request
        existing_codes = tuple(existing_replan.reason_codes) if existing_replan else ()
        existing_details = tuple(existing_replan.detail) if existing_replan else ()
        validated = replace(
            validated,
            plan_valid=False,
            issues=tuple(validated.issues) + tuple(
                code for code in proof_audit["errors"] if code not in validated.issues
            ),
            replan_request=ReplanRequest(
                reason_codes=tuple(dict.fromkeys(existing_codes + tuple(proof_audit["errors"]))),
                detail=tuple(dict.fromkeys(existing_details + tuple(proof_audit["errors"]))),
                affected_chapter_ids=existing_replan.affected_chapter_ids if existing_replan else (),
            ),
        )
    retained_claims = [item.claim for item in ranking.retained_values]
    assessment = dict(validated.duration_assessment or {})
    assessment["commerce_lite_draft_rank_final"] = {
        "stage": "draft_to_commercial_ranking_to_final_v1",
        "draft_step_count": len(draft.buying_path),
        "draft_grounding": draft_grounding,
        "retained_value_count": len(ranking.retained_values),
        "retained_claims": retained_claims,
        "dropped_draft_ids": list(ranking.dropped_draft_ids),
        "ranking_grounding": ranking_grounding,
        "minimal_proof_audit": proof_audit,
        "final_chapter_count": len(validated.beats),
        "final_plan_valid": validated.plan_valid,
        "boundary": "ranking_prioritizes_values_only_final_is_only_m3_input",
    }
    return draft, ranking, replace(validated, duration_assessment=assessment)


# ---------------------------------------------------------------------------
# P0: Strong Clip Ranking -> Purchase Cognition Composition
#
# This is intentionally kept inside the existing experimental M2 module.  It
# does not alter M1 discovery, Director Strategy generation, the Ledger, or
# M3.  In particular, neither helper deletes from the hard-safe pool: the
# first helper only exposes M1-linked evidence to a comparative model task and
# the second submits the model's exact selected IDs to the common validator.


def _strategy_related_tags(
    tags: Sequence[CommerceLiteTag], *, strategy: Any, limit: int = 40,
) -> tuple[CommerceLiteTag, ...]:
    """Return the selected strategy's explicitly linked executable evidence.

    This is a recall view for M2, not a new candidate whitelist or local rank.
    It is deliberately based only on M1 tier lineage already present in the
    read-only tag projection.  If a story has fewer than 20 grounded assets we
    preserve that fact rather than filling the request with unrelated lines.
    """
    cap = max(1, int(limit))
    linked = [tag for tag in tags if tag.materializable and tag.m1_tiers]
    if len(linked) >= cap:
        return tuple(linked[:cap])

    # M1 evidence is often sparse: it may name an opening mechanism but omit
    # a stronger later spoken result from the same story.  Use the *existing*
    # M1 text only to recall additional candidates for comparison.  This is
    # intentionally generic lexical retrieval (not a fashion keyword table,
    # not a score that reaches the final plan, and never a candidate filter).
    facts = [
        _text(getattr(strategy, name, ""))
        for name in ("thesis", "story_premise", "audience_tension", "core_commercial_idea", "payoff")
    ]
    for attr in ("core_evidence_pool", "supporting_evidence_pool", "bridge_candidates", "evidence"):
        for item in tuple(getattr(strategy, attr, ()) or ()):
            facts.append(_text(getattr(item, "claim", "")))
    normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", "".join(facts).lower())
    bigrams = {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}
    chars = set(normalized)

    def recall_score(tag: CommerceLiteTag) -> tuple[int, int, int]:
        text = re.sub(r"[^\w\u4e00-\u9fff]", "", tag.text.lower())
        text_bigrams = {text[index:index + 2] for index in range(max(0, len(text) - 1))}
        # Tuple ordering is only for bounded recall; Strong Clip Ranking owns
        # every editorial comparison after this function returns.
        return (len(bigrams.intersection(text_bigrams)), len(chars.intersection(set(text))), -tag.candidate_id)

    # Opening eligibility is an immutable production fact, not a commercial
    # score.  Include every legally independent opening in the comparative
    # view before bounded M1-text recall so the later local Q1 quality pass is
    # not pre-blocked by a sparse M1 evidence list.
    legal_openings = [
        tag for tag in tags
        if tag.materializable and tag.hook_eligible and tag.candidate_id not in {item.candidate_id for item in linked}
    ]
    known_ids = {tag.candidate_id for tag in (*linked, *legal_openings)}
    recalled = sorted(
        (tag for tag in tags if tag.materializable and tag.candidate_id not in known_ids),
        key=recall_score, reverse=True,
    )
    return tuple((linked + legal_openings + recalled)[:cap])


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"true", "1", "yes", "是"}


def _director_opening_scope(
    director_strategy_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return an Archetype-owned opening boundary, never a candidate choice."""
    contract = dict(director_strategy_contract or {})
    raw = contract.get("opening_scope")
    if not isinstance(raw, Mapping):
        # Pre-P0.1 callers retain the frozen generic Hook contract.
        return {
            "allowed_purchase_question_ids": list(PURCHASE_JOURNEY_BY_ID),
            "allowed_answer_roles": sorted({
                role for item in PURCHASE_JOURNEY_QUESTIONS
                for role in item.get("allowed_answer_roles") or ()
            }),
            "fallback_to_global_opening": True,
            "requires_clean_independent_utterance": True,
            "archetype": "",
        }
    question_ids = [
        question_id for question_id in (
            _text(item) for item in raw.get("allowed_purchase_question_ids") or ()
        ) if question_id in PURCHASE_JOURNEY_BY_ID
    ]
    allowed_roles = [
        role for role in (_text(item).lower() for item in raw.get("allowed_answer_roles") or ())
        if role
    ]
    if not question_ids or not allowed_roles:
        raise ValueError("Narrative Archetype opening_scope 缺少正式购买问题或 answer_role")
    return {
        "allowed_purchase_question_ids": list(dict.fromkeys(question_ids)),
        "allowed_answer_roles": list(dict.fromkeys(allowed_roles)),
        "fallback_to_global_opening": _as_bool(raw.get("fallback_to_global_opening")),
        "requires_clean_independent_utterance": _as_bool(raw.get("requires_clean_independent_utterance")),
        "archetype": _text(contract.get("narrative_archetype")).lower(),
    }


def _director_early_journey_scope(
    director_strategy_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Read Archetype journey priority as data for existing M2 operations."""
    contract = dict(director_strategy_contract or {})
    raw = contract.get("early_journey_scope")
    if not isinstance(raw, Mapping):
        return {
            "opening_question_ids": ["Q1", "Q2"],
            "required_question_ids": ["Q1", "Q2"],
            "recommended_question_ids": ["Q3", "Q4", "Q5", "Q6"],
            "optional_question_ids": ["Q7"],
            "preferred_question_order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
        }

    def formal_list(key: str) -> list[str]:
        return list(dict.fromkeys(
            question_id for question_id in (_text(item) for item in raw.get(key) or ())
            if question_id in PURCHASE_JOURNEY_BY_ID
        ))

    opening_ids = formal_list("opening_question_ids")
    required_ids = formal_list("required_question_ids")
    recommended_ids = formal_list("recommended_question_ids")
    optional_ids = formal_list("optional_question_ids")
    preferred_ids = formal_list("preferred_question_order")
    if len(opening_ids) != 2 or not set(opening_ids).issubset(required_ids):
        raise ValueError("Narrative Archetype early_journey_scope 必须声明两个 required Opening 问题")
    if not preferred_ids:
        preferred_ids = list(dict.fromkeys((*opening_ids, *required_ids, *recommended_ids, *optional_ids)))
    return {
        "opening_question_ids": opening_ids,
        "required_question_ids": required_ids,
        "recommended_question_ids": recommended_ids,
        "optional_question_ids": optional_ids,
        "preferred_question_order": preferred_ids,
    }


def build_strong_clip_ranking_prompt(
    *, strategy: Any, tags: Sequence[CommerceLiteTag], director_strategy_contract: Mapping[str, Any],
    retry_reason: str = "",
) -> str:
    """Ask for a relative editorial ranking, never a completed clip plan."""
    opening_scope = _director_opening_scope(director_strategy_contract)
    return "\n".join((
        "你在做 M2 的 Strong Clip Ranking。这是选句判断，不是写脚本、不是章节覆盖、不是最终片单。",
        "只比较下列当前导演方案相关、可物化的原字幕候选。不得创作、改写字幕，不能引用列表外 candidate_id。",
        "每条 Candidate Unit 已带有 M1 claim/story role、Ledger asset role 与 visual_role_hints。visual_role_hints 只是字幕/资产推断，",
        "不是帧级视觉事实；不能据此虚构转身、手摸或实际对比画面。",
        "逐条判断：standalone_strength（单独占3秒值不值）、hook_power（陌生用户会不会停留）、",
        "purchase_value（它给出的具体购买理由）、purchase_outcome（它实际解决的一个具体购买结果）、",
        "purchase_question_id（必须从下方正式购买问题表中选择）、purchase_question（原样使用表内问题）、",
        "supports_question_id（该句在证明前面哪个问题；例如 Q2 的肩部机制 supports Q1；无则空字符串）、",
        "answer_role（result/mechanism/proof/risk_remove/comfort/scene/styling/trust 中的一项）。",
        "purchase_question_role、answered_question、evidence_function 仅为兼容旧报告的同义字段：分别与 answer_role、purchase_question、answer_role 对齐填写。",
        "proof_strength（数字/机制/结果/细节而非空夸）、",
        "redundancy_group（与哪些候选实际上讲同一件事）、fragment、visual_dependency。",
        "visual_dependency=true 不是淘汰，它仍可作为证明；fragment=true 也不等于没价值，但不能作为独立开场。",
        "排序优先：具体消费者结果或反差 > 可验证机制/数字/结果 > 泛化商品介绍或直播过程话。",
        "不要因为一句提到商品就排前面；不要把同一肩部机制的多句近义表达排成多个强入口。",
        "purchase_outcome 要把“肩变窄”和“腰胯遮肉”分开；同义肩部机制必须同属一个 outcome。",
        "同一 purchase_question_id + answer_role + purchase_outcome 只能留一个最强候选；不要把两句肩部机制同时留下。",
        "answer_role 只描述它对购买问题的作用；同一结果不要把两句近义 mechanism 伪装成不同作用。",
        "候选字段 hook_eligible=false 是冻结的开场权限事实：即使内容价值高也绝不能给 opening_rank。",
        "另给每条候选 opening_rank：只给 hook_eligible=true、能独立开场、且符合本 Archetype opening_scope 的候选从 1 开始唯一排序；fragment 或 visual_dependency=true 必须为 0。",
        "opening_rank=1 是当前方案唯一优先开场；它必须优先于 M1 Hero 的第一条证据，不能因为故事主线或 bridge 身份自动继承开场。",
        "opening_scope 是正式边界：只有 purchase_question_id 和 answer_role 同时落入下列范围才可写 opening_rank。范围内没有干净独立入口时，所有 opening_rank=0；不得退回全局显瘦 Hook 或其他购买问题伪装成场景入口。",
        ("上一版违反了 Opening 合同：" + _text(retry_reason) + "。只重做同一候选池的排序；不得给无权限、残句或依赖画面的候选 opening_rank。") if retry_reason else "",
        "本次唯一允许写入 opening_rank 的 candidate_id（已知 ASR/残句 final block 同样不能当 Opening）：" + json.dumps(
            [
                tag.candidate_id for tag in tags
                if tag.hook_eligible and not _quality_final_utterance_reject_reason(tag.text)
            ], ensure_ascii=False, separators=(",", ":"),
        ),
        "当前 Archetype opening_scope：" + json.dumps(opening_scope, ensure_ascii=False, separators=(",", ":")),
        "完整输入仅用于比较；只返回最值得进入下一步的 Top 12（输入不足12条则全部返回），名次必须唯一。",
        "不要逐条解释其余候选，避免用冗长 JSON 挤掉真正的排序结果。选择理由每条最多一句，直接说它为什么值得占据成片。",
        "M1 固定故事事实：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前用户选择的导演方案（只提供销售目标和故事血缘，不替代 M1 主承诺）：",
        json.dumps(dict(director_strategy_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "正式购买问题关系表：",
        json.dumps(_purchase_journey_question_payload(), ensure_ascii=False, separators=(",", ":")),
        "当前方案相关候选：",
        json.dumps([tag.to_dict() for tag in tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "ranking_summary": "",
            "ranked_candidates": [{
                "candidate_id": 0, "rank": 1,
                "standalone_strength": 0, "hook_power": 0,
                "purchase_value": "", "purchase_outcome": "", "purchase_question_id": "", "purchase_question": "", "supports_question_id": "", "answer_role": "", "purchase_question_role": "", "answered_question": "", "evidence_function": "",
                "proof_strength": 0,
                "redundancy_group": "", "fragment": False,
                "visual_dependency": False, "opening_rank": 0, "opening_reason": "", "selection_reason": "",
            }],
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _parse_strong_clip_ranking(
    data: Mapping[str, Any], *, candidate_ids: set[int], hook_eligible_ids: set[int],
    opening_scope: Mapping[str, Any] | None = None,
) -> tuple[tuple[StrongClipRank, ...], dict[str, Any]]:
    scope = dict(opening_scope or _director_opening_scope(None))
    scope_question_ids = {
        _text(item) for item in scope.get("allowed_purchase_question_ids") or () if _text(item)
    }
    scope_answer_roles = {
        _text(item).lower() for item in scope.get("allowed_answer_roles") or () if _text(item)
    }
    raw = data.get("ranked_candidates") or data.get("ranking") or ()
    if isinstance(raw, Mapping):
        raw = (raw,)
    parsed: list[StrongClipRank] = []
    seen: set[int] = set()
    relation_rejected: list[int] = []
    for index, item in enumerate(raw if isinstance(raw, Sequence) else (), 1):
        if not isinstance(item, Mapping):
            continue
        ids = _as_int_tuple(item.get("candidate_id"))
        candidate_id = ids[0] if ids else 0
        if candidate_id not in candidate_ids or candidate_id in seen:
            continue
        question_id = _text(item.get("purchase_question_id"))
        question = _text(item.get("purchase_question")) or _text(item.get("answered_question"))
        supports_question_id = _text(item.get("supports_question_id"))
        answer_role = (
            _text(item.get("answer_role")).lower()
            or _text(item.get("evidence_function")).lower()
            or _text(item.get("purchase_question_role")).lower()
        )
        spec = PURCHASE_JOURNEY_BY_ID.get(question_id)
        if (
            spec is None
            or question != _text(spec.get("purchase_question"))
            or supports_question_id != _text(spec.get("supports_question_id"))
            or answer_role not in set(spec.get("allowed_answer_roles") or ())
        ):
            relation_rejected.append(candidate_id)
            continue
        seen.add(candidate_id)
        parsed.append(StrongClipRank(
            candidate_id=candidate_id,
            rank=max(1, int(_number(item.get("rank")) or index)),
            standalone_strength=round(min(10.0, max(0.0, _number(item.get("standalone_strength")))), 2),
            hook_power=round(min(10.0, max(0.0, _number(item.get("hook_power")))), 2),
            purchase_value=_text(item.get("purchase_value")),
            purchase_outcome=_text(item.get("purchase_outcome")),
            purchase_question_id=question_id,
            purchase_question=question,
            supports_question_id=supports_question_id,
            answer_role=answer_role,
            purchase_question_role=_text(item.get("purchase_question_role")).lower(),
            answered_question=_text(item.get("answered_question")),
            evidence_function=_text(item.get("evidence_function")).lower(),
            proof_strength=round(min(10.0, max(0.0, _number(item.get("proof_strength")))), 2),
            redundancy_group=_text(item.get("redundancy_group")) or f"ungrouped_{candidate_id}",
            fragment=_as_bool(item.get("fragment")),
            visual_dependency=_as_bool(item.get("visual_dependency")),
            opening_rank=max(0, int(_number(item.get("opening_rank")))),
            opening_reason=_text(item.get("opening_reason")),
            selection_reason=_text(item.get("selection_reason")),
        ))
    parsed.sort(key=lambda item: (item.rank, item.candidate_id))
    if not parsed:
        raise ValueError("Strong Clip Ranking 没有返回可用候选排序")
    ranks = [item.rank for item in parsed]
    if len(set(ranks)) != len(ranks):
        raise ValueError("Strong Clip Ranking 的 rank 必须唯一")
    opening_rows = [item for item in parsed if item.opening_rank > 0]
    if len({item.opening_rank for item in opening_rows}) != len(opening_rows):
        raise ValueError("Strong Clip Ranking 的 opening_rank 必须唯一")
    invalid_opening_rows = [
        item.candidate_id for item in opening_rows
        if item.fragment or item.visual_dependency or item.candidate_id not in hook_eligible_ids
    ]
    if invalid_opening_rows:
        raise ValueError(
            "Strong Clip Ranking 将无开场权限、残句或依赖画面的候选列为 Opening："
            + ",".join(map(str, invalid_opening_rows))
        )
    outside_scope_rows = [
        item.candidate_id for item in opening_rows
        if item.purchase_question_id not in scope_question_ids
        or _rank_answer_role(item).lower() not in scope_answer_roles
    ]
    if outside_scope_rows:
        raise ValueError(
            "Strong Clip Ranking 将范围外购买问题列为 Opening："
            + ",".join(map(str, outside_scope_rows))
        )
    return tuple(parsed), {
        "ranking_summary": _text(data.get("ranking_summary")),
        "input_candidate_count": len(candidate_ids),
        "returned_top_candidate_count": len(parsed),
        "not_returned_candidate_ids": sorted(candidate_ids - seen),
        "ranked_candidate_ids": [item.candidate_id for item in parsed],
        "opening_ranked_candidate_ids": [
            item.candidate_id for item in sorted(opening_rows, key=lambda row: row.opening_rank)
        ],
        "opening_scope": dict(scope),
        "opening_scope_available": bool(opening_rows),
        "relation_rejected_candidate_ids": relation_rejected,
        "boundary": "relative_model_ranking_only_no_candidate_mutation_or_local_reorder",
    }


def build_purchase_cognition_composition_prompt(
    *, strategy: Any, ranked: Sequence[StrongClipRank], tags: Sequence[CommerceLiteTag],
    target_duration: float, director_strategy_contract: Mapping[str, Any] | None,
) -> str:
    """Compose a natural purchase-cognition path from already ranked clips."""
    early_scope = _director_early_journey_scope(director_strategy_contract)
    tag_by_id = {tag.candidate_id: tag for tag in tags}
    ranked_payload = [
        {**item.to_dict(), "candidate": tag_by_id[item.candidate_id].to_dict()}
        for item in ranked if item.candidate_id in tag_by_id
    ]
    opening_ranked = [
        item for item in sorted(ranked, key=lambda row: row.opening_rank or 10_000)
        if item.opening_rank > 0 and not item.fragment and not item.visual_dependency
        and tag_by_id.get(item.candidate_id)
    ]
    return "\n".join((
        "你在做 M2 的 Purchase Journey Composition。Strong Clip Ranking 已完成；现在只用其中排序过的真实候选组成视频。",
        "必须先输出 purchase_question_route（只写 question_id、question、journey_role、why_now，绝对不能写 candidate_id），再为路线逐问选择证据生成 chapters。",
        "路线只能使用下方正式购买问题表中、且在强句排序里有真实候选的问题。chapters 按路线顺序回答已有 question_id，不得临时创造问题、跳过路线或把多个问题塞进同一章。",
        "你不是按 Hook/机制/结果等章节名凑覆盖，也不能重新发现第四个故事。你的唯一组织目标是 purchase_cognition_path：",
        "观众的购买认知要持续向前。每个新候选只能二选一：A. 为当前认知提供更强且必要的证明；B. 带来新的购买认知。两者都不是就删。",
        "把 chapter 理解为 Question Step，不是卖点标题：每一步先让一个购买问题成立，再用最小充分证据回答它。",
        "同一 purchase_outcome 默认最小充分证据：最多三条；同一 purchase_question_id + answer_role + purchase_outcome 只允许一条。不得用 33+251 之类的同义肩部 mechanism 拖长视频。",
        "每一个 chapter 必须匹配候选的 purchase_question_id，回答一个新的 purchase_question。Q2 若出现，必须填写 supports_question_id=Q1 的候选来兑现 Q1；后续新章节必须是真正新的购买问题。",
        "Opening 从强句池独立选择，不等于 M1 Hero 的第一条证据。C1 的 hook_candidate_ids 必须严格使用 opening_rank=1 的唯一候选；C2 必须立即兑现 C1，",
        "不能把 fragment、依赖画面的承接句、直播过程话硬当开场。开头的承诺、受众、结果要让陌生用户听得懂。",
        "当前 Archetype 的 early_journey_scope 是正式导演路径：purchase_question_route 的前两题必须严格按 opening_question_ids，C1/C2 分别回答它们。required 是必须优先建立的路径；recommended 仅在有真实强候选时加入；optional 可主动不讲。不得把其它全局高分 Hook 偷换进 C1。",
        "当前 early_journey_scope：" + json.dumps(early_scope, ensure_ascii=False, separators=(",", ":")),
        "这是核心故事初稿，不是最终停止判断。目标时长只是软目标；若本稿仍短，程序会按未覆盖购买问题从完整安全池定向召回。不得声称完整安全池不足。",
        "不得改写字幕、不得使用未排序候选、不得复用 candidate_id。允许短片，但必须在 duration_assessment 中如实说明本稿只完成了哪些问题。",
        "story_consumption 必须声明当前 strategy_id，且 supporting/bridge 只在实际使用时声明；不得凭空声明。",
        f"目标时长 {max(1.0, float(target_duration)):.1f}s（软目标）。",
        "M1 固定故事事实：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前用户选择的导演方案：",
        json.dumps(dict(director_strategy_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "正式购买问题关系表：",
        json.dumps(_purchase_journey_question_payload(), ensure_ascii=False, separators=(",", ":")),
        "独立 Opening 排名（第一名必须作为 C1 Hook）：",
        json.dumps([item.to_dict() for item in opening_ranked], ensure_ascii=False, separators=(",", ":")),
        "强句排序与原字幕：",
        json.dumps(ranked_payload, ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "purchase_question_route": [{
                "question_id": "Q1", "question": "", "journey_role": "why_buy/why_believe/fit/comfort/usage/value",
                "why_now": "",
            }],
            "opening_package": {
                "hook_promise": "", "payoff_delivery": "", "connection_reason": "",
                "hook_candidate_ids": [], "payoff_candidate_ids": [], "hook_integrity_reason": "",
            },
            "purchase_cognition_path": [{
                "step_id": "P1", "chapter_id": "C1", "purchase_cognition": "",
                "purchase_question_id": "Q1", "purchase_question": "", "supports_question_id": "", "answer_role": "result/mechanism/proof/risk_remove/comfort/scene/styling/trust", "answered_question": "", "advance_type": "new_purchase_cognition/necessary_stronger_proof",
                "candidate_ids": [], "why_it_advances": "",
            }],
            "chapters": [{
                "chapter_id": "C1", "narrative_role": "hook/payoff/proof/new_value/close",
                "goal": "", "candidate_ids": [], "asset_tier": "core/supporting/bridge/safe_reserve",
                "story_support": "", "commerce_beat_id": "", "value_dimension": "",
                "purchase_value_dimension": "new_outcome/same_claim_additional_proof",
                "purchase_value_domain": "", "purchase_value_outcomes": [], "purchase_value_reason": "",
            }],
            "story_consumption": {
                "hero_strategy_id": "", "hero_priority": "", "hero_consistency_reason": "",
                "supporting_chapter_ids": [], "bridge_chapter_ids": [], "no_rediscovery": True,
                "supporting_candidate_ids": [], "bridge_candidate_ids": [],
            },
            "duration_assessment": {"status": "full/insufficient_material", "reason": ""},
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _with_measured_beat_seconds(
    beats: Sequence[Any], safe_candidates: Sequence[PlanningCandidate],
) -> tuple[Any, ...]:
    duration_by_id = {candidate.candidate_id: candidate.duration for candidate in safe_candidates}
    return tuple(replace(
        beat,
        target_seconds=round(sum(duration_by_id.get(candidate_id, 0.0) for candidate_id in beat.candidate_ids), 3),
    ) for beat in beats)


def _purchase_path_audit(
    data: Mapping[str, Any], *, beats: Sequence[Any], ranked: Sequence[StrongClipRank],
) -> dict[str, Any]:
    """Verify that M2 actually progresses buyer questions, not chapter labels.

    The model remains responsible for editorial interpretation.  This audit
    only checks the plan against the ranker's own declared outcome, question,
    and evidence-function annotations, so it cannot turn into a category
    keyword blacklist or select replacement clips.
    """
    raw_path = data.get("purchase_cognition_path") or ()
    if isinstance(raw_path, Mapping):
        raw_path = (raw_path,)
    raw_route = data.get("purchase_question_route") or ()
    if isinstance(raw_route, Mapping):
        raw_route = (raw_route,)
    by_chapter = {beat.chapter_id: beat for beat in beats}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    rank_by_id = {item.candidate_id: item for item in ranked}
    ranked_ids = set(rank_by_id)
    route_rows: list[dict[str, str]] = []
    route_by_id: dict[str, dict[str, str]] = {}
    for index, item in enumerate(raw_route if isinstance(raw_route, Sequence) else (), 1):
        if not isinstance(item, Mapping):
            errors.append(f"purchase_question_route_invalid_row:{index}")
            continue
        question_id = _text(item.get("question_id"))
        question = _text(item.get("question"))
        if not question_id or not question:
            errors.append(f"purchase_question_route_missing_fields:{index}")
            continue
        if question_id in route_by_id:
            errors.append(f"purchase_question_route_duplicate_id:{question_id}")
            continue
        route_row = {
            "question_id": question_id,
            "question": question,
            "journey_role": _text(item.get("journey_role")),
            "why_now": _text(item.get("why_now")),
        }
        route_by_id[question_id] = route_row
        route_rows.append(route_row)
    if not route_rows:
        errors.append("purchase_question_route_missing")
    selected_by_outcome: dict[str, list[int]] = {}
    selected_by_outcome_function: dict[tuple[str, str], list[int]] = {}
    selected_by_question_role_outcome: dict[tuple[str, str, str], list[int]] = {}
    question_state: dict[str, dict[str, Any]] = {}
    materialized_question_ids: list[str] = []
    candidate_relations: list[dict[str, Any]] = []
    for index, item in enumerate(raw_path if isinstance(raw_path, Sequence) else (), 1):
        if not isinstance(item, Mapping):
            errors.append(f"purchase_path_invalid_row:{index}")
            continue
        chapter_id = _text(item.get("chapter_id"))
        candidate_ids = _as_int_tuple(item.get("candidate_ids"))
        advance_type = _text(item.get("advance_type"))
        cognition = _text(item.get("purchase_cognition"))
        purchase_question_id = _text(item.get("purchase_question_id"))
        answered_question = _text(item.get("answered_question"))
        purchase_question = _text(item.get("purchase_question")) or answered_question
        supports_question_id = _text(item.get("supports_question_id"))
        answer_role = _text(item.get("answer_role")).lower()
        beat = by_chapter.get(chapter_id)
        if beat is None or tuple(candidate_ids) != tuple(beat.candidate_ids):
            errors.append(f"purchase_path_chapter_mismatch:{chapter_id or index}")
            continue
        if any(candidate_id not in ranked_ids for candidate_id in candidate_ids):
            errors.append(f"purchase_path_unranked_candidate:{chapter_id}")
        candidate_question_ids = {
            _text(rank_by_id[candidate_id].purchase_question_id)
            for candidate_id in candidate_ids if candidate_id in rank_by_id
        }
        candidate_question_ids.discard("")
        route = route_by_id.get(purchase_question_id)
        if not purchase_question_id:
            errors.append(f"purchase_path_question_id_missing:{chapter_id}")
        elif route is None:
            errors.append(f"purchase_path_question_not_in_route:{chapter_id}:{purchase_question_id}")
        elif candidate_question_ids != {purchase_question_id}:
            errors.append(f"purchase_path_question_id_not_grounded:{chapter_id}")
        elif len(rows) >= len(route_rows) or purchase_question_id != route_rows[len(rows)]["question_id"]:
            errors.append(f"purchase_path_route_order_changed:{chapter_id}")
        if not answered_question:
            errors.append(f"purchase_path_answered_question_missing:{chapter_id}")
        elif route is not None and answered_question != route["question"]:
            errors.append(f"purchase_path_question_not_grounded:{chapter_id}")
        elif candidate_ids and any(
            _text(rank_by_id[candidate_id].answered_question) != answered_question
            for candidate_id in candidate_ids if candidate_id in rank_by_id
        ):
            errors.append(f"purchase_path_question_not_grounded:{chapter_id}")
        if not purchase_question:
            errors.append(f"purchase_path_purchase_question_missing:{chapter_id}")
        elif candidate_ids and any(
            _rank_purchase_question(rank_by_id[candidate_id]) != purchase_question
            for candidate_id in candidate_ids if candidate_id in rank_by_id
        ):
            errors.append(f"purchase_path_purchase_question_not_grounded:{chapter_id}")
        # A Quality micro-sequence is one buyer-question chapter: its first
        # candidate is the anchor named by the path row, while later M2-picked
        # lines may intentionally have different evidence functions.  This
        # audit verifies the anchor declaration and each support relation
        # below; it never assigns a support or rewrites its role.
        if not answer_role and candidate_ids:
            answer_role = _rank_answer_role(rank_by_id[candidate_ids[0]])
        if not answer_role:
            errors.append(f"purchase_path_answer_role_missing:{chapter_id}")
        elif candidate_ids and candidate_ids[0] in rank_by_id and (
            _rank_answer_role(rank_by_id[candidate_ids[0]]) != answer_role
        ):
            errors.append(f"purchase_path_answer_role_not_grounded:{chapter_id}")
        if not supports_question_id and candidate_ids:
            supports_question_id = _text(getattr(rank_by_id[candidate_ids[0]], "supports_question_id", ""))
        if candidate_ids and any(
            _text(getattr(rank_by_id[candidate_id], "supports_question_id", "")) != supports_question_id
            for candidate_id in candidate_ids if candidate_id in rank_by_id
        ):
            errors.append(f"purchase_path_supports_question_not_grounded:{chapter_id}")
        if supports_question_id and supports_question_id not in materialized_question_ids:
            errors.append(f"purchase_path_supports_question_not_previously_answered:{chapter_id}:{supports_question_id}")
        outcomes = set(beat.purchase_value_outcomes)
        if advance_type not in {"new_purchase_cognition", "necessary_stronger_proof"}:
            errors.append(f"purchase_path_advance_type_invalid:{chapter_id}")
        if not cognition:
            errors.append(f"purchase_path_cognition_missing:{chapter_id}")
        for candidate_id in candidate_ids:
            rank = rank_by_id.get(candidate_id)
            if rank is None:
                continue
            outcome = _text(rank.purchase_outcome)
            evidence_function = _text(rank.evidence_function)
            if not outcome:
                errors.append(f"purchase_path_outcome_missing:{chapter_id}:{candidate_id}")
                continue
            if not evidence_function:
                errors.append(f"purchase_path_evidence_function_missing:{chapter_id}:{candidate_id}")
                continue
            selected_by_outcome.setdefault(outcome, []).append(candidate_id)
            selected_by_outcome_function.setdefault((outcome, evidence_function), []).append(candidate_id)
            candidate_answer_role = _rank_answer_role(rank)
            selected_by_question_role_outcome.setdefault(
                (purchase_question_id, candidate_answer_role, outcome), []
            ).append(candidate_id)
            candidate_relations.append({
                "candidate_id": candidate_id,
                "purchase_question_id": purchase_question_id,
                "purchase_question": purchase_question,
                "supports_question_id": supports_question_id,
                "answer_role": candidate_answer_role,
                "purchase_outcome": outcome,
            })
            if purchase_question_id:
                state = question_state.setdefault(purchase_question_id, {
                    "question": answered_question, "journey_role": route["journey_role"] if route else "",
                    "candidate_ids": [], "purchase_outcomes": [], "evidence_functions": [],
                })
                if candidate_id not in state["candidate_ids"]:
                    state["candidate_ids"].append(candidate_id)
                if outcome not in state["purchase_outcomes"]:
                    state["purchase_outcomes"].append(outcome)
                if evidence_function not in state["evidence_functions"]:
                    state["evidence_functions"].append(evidence_function)
        rows.append({
            "step_id": _text(item.get("step_id")) or f"P{index}", "chapter_id": chapter_id,
            "candidate_ids": list(candidate_ids), "purchase_cognition": cognition,
            "purchase_question_id": purchase_question_id, "purchase_question": purchase_question,
            "supports_question_id": supports_question_id, "answer_role": answer_role,
            "answered_question": answered_question,
            "advance_type": advance_type, "why_it_advances": _text(item.get("why_it_advances")),
        })
        if purchase_question_id:
            materialized_question_ids.append(purchase_question_id)
    if len(rows) != len(beats):
        errors.append("purchase_path_does_not_cover_exact_plan_chapters")
    if [row["question_id"] for row in route_rows] != [row["purchase_question_id"] for row in rows]:
        errors.append("purchase_question_route_not_materialized_exactly_once")
    for outcome, candidate_ids in selected_by_outcome.items():
        if len(candidate_ids) > 3:
            errors.append("purchase_outcome_evidence_cap_exceeded:" + outcome)
    for (outcome, evidence_function), candidate_ids in selected_by_outcome_function.items():
        if len(candidate_ids) > 1:
            errors.append(
                "purchase_outcome_function_repeated:" + outcome + ":" + evidence_function
            )
    for (question_id, answer_role, outcome), candidate_ids in selected_by_question_role_outcome.items():
        if len(candidate_ids) > 1:
            errors.append(
                "purchase_question_answer_role_outcome_repeated:"
                + question_id + ":" + answer_role + ":" + outcome
            )
    question_states = []
    for question_id, state in question_state.items():
        functions = set(state["evidence_functions"])
        # A result plus an explanation is a completed persuasion loop.  A
        # standalone support (comfort, trust, scene) can also answer its own
        # independent buying question.  This is reporting only; selection is
        # constrained above by no repeated questions and proof saturation.
        satisfied = bool(
            {"result", "mechanism"}.issubset(functions)
            or {"result", "extension"}.issubset(functions)
            or functions.intersection({"support", "comfort", "risk_removal", "scene", "trust"})
        )
        question_states.append({
            "purchase_question_id": question_id,
            "answered_question": state["question"],
            **state,
            "state": "satisfied" if satisfied else "answered_not_yet_multi_evidence",
        })
    return {
        "passed": not errors,
        "errors": list(dict.fromkeys(errors)),
        "steps": rows,
        "purchase_question_route": route_rows,
        "purchase_question_state": question_states,
        "candidate_relations": candidate_relations,
    }


def _missing_purchase_questions(path_audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compute coverage only from M2's selected formal question IDs."""
    covered = {
        _text(item.get("purchase_question_id"))
        for item in path_audit.get("steps") or ()
        if isinstance(item, Mapping) and _text(item.get("purchase_question_id"))
    }
    return [
        dict(item) for item in PURCHASE_JOURNEY_QUESTIONS
        if str(item["purchase_question_id"]) not in covered
    ]


def _targeted_recall_candidates(
    tags: Sequence[CommerceLiteTag], *, selected_ids: set[int],
) -> tuple[CommerceLiteTag, ...]:
    """Expose every remaining executable-safe candidate in source order.

    This function intentionally does no semantic scoring, product filtering,
    or selection. The M2 model receives the whole remaining safe pool and is
    solely responsible for its small per-question recall list and final choice.
    """
    return tuple(
        tag for tag in tags
        if tag.materializable and tag.candidate_id not in selected_ids
    )


def build_purchase_journey_targeted_recall_prompt(
    *,
    strategy: Any,
    director_strategy_contract: Mapping[str, Any] | None,
    target_duration: float,
    initial_path_audit: Mapping[str, Any],
    missing_questions: Sequence[Mapping[str, Any]],
    unconsumed_tags: Sequence[CommerceLiteTag],
) -> str:
    """Ask M2 to recall and choose new buyer-value evidence from the full pool."""
    missing_ids = [str(item.get("purchase_question_id") or "") for item in missing_questions]
    missing_payload = _recall_question_payload(missing_questions)
    initial_steps = [
        item for item in initial_path_audit.get("steps") or ()
        if isinstance(item, Mapping)
    ]
    chapter_numbers = [
        int(match.group(1)) for item in initial_steps
        for match in [re.fullmatch(r"C(\d+)", _text(item.get("chapter_id")), flags=re.I)]
        if match
    ]
    step_numbers = [
        int(match.group(1)) for item in initial_steps
        for match in [re.fullmatch(r"P(\d+)", _text(item.get("step_id")), flags=re.I)]
        if match
    ]
    next_chapter_id = f"C{max(chapter_numbers, default=0) + 1}"
    next_step_id = f"P{max(step_numbers, default=0) + 1}"
    return "\n".join((
        "你仍在同一个 M2 Purchase Journey 中。核心故事初稿已完成，但程序根据已选 candidate 的 formal purchase_question_id，或 Director Blueprint 的高价值章节顺序，发现还有未探索购买价值。",
        "现在必须回到下方【完整剩余 executable-safe candidate pool】做定向召回。这个池不是 Strong Ranking Top 12，也不是 M1 关联白名单。",
        "程序不替你判断任何候选语义：你先为每个缺失问题列出最多 5 条最相关、同一主商品、真实且未使用的候选，再从各自列出的候选中选择至多一条加入。",
        "若候选只重复已回答问题、属于其他商品、是残缺或没有新的购买价值，不能列入。若一个缺失问题没有真实可用候选，candidates 留空并说明原因。",
        "每条 recall 必须声明 purchase_question_id、purchase_question、supports_question_id、answer_role、purchase_outcome。关系必须使用下方正式表或 Blueprint slot 的 allowed_answer_roles；不得新造问题或把同义肩部机制伪装成新章节。",
        "选择规则：只追加一个新的购买问题，或对已经出现的问题提供必要但不同 answer_role 的新购买价值。不得改写、替换、删除、重排初稿；不得为了目标秒数凑内容。",
        f"初稿已冻结章节为 {[item.get('chapter_id') for item in initial_steps]}。所有 append_chapters / append_purchase_cognition_path 必须是全新的连续章节号，第一条必须是 {next_chapter_id}/{next_step_id}；绝不能复用初稿章节号。",
        "Q5 若召回里同时存在正向解除方案和单纯限制/警告，优先选择弹力、里衬、不用安全裤、耐穿等正向解除方案；不要把使用限制当作最强购买理由。",
        "如果任何缺失问题的 recall_candidates 非空，就必须选择其中最强且不重复的一条；只有完整剩余池对所有缺失问题均无合格候选时，才可报告 source_material_insufficient。",
        "仍然保留现有 Hook：不得把新召回候选改成 Opening，也不得破坏 hook_eligible 合同。",
        f"目标时长 {max(1.0, float(target_duration)):.1f}s（软目标）。",
        "M1 固定故事事实：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前用户选择的导演方案：",
        json.dumps(dict(director_strategy_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "初稿已选购买问题与候选关系（不得变更）：",
        json.dumps({
            "steps": list(initial_path_audit.get("steps") or ()),
            "candidate_relations": list(initial_path_audit.get("candidate_relations") or ()),
        }, ensure_ascii=False, separators=(",", ":")),
        "本轮未探索购买问题 / Blueprint 章节：",
        json.dumps(missing_payload, ensure_ascii=False, separators=(",", ":")),
        "完整剩余 executable-safe candidate pool：",
        json.dumps([tag.to_dict() for tag in unconsumed_tags], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "missing_purchase_questions": missing_payload,
            "recall_by_question": [{
                "purchase_question_id": "Q3", "purchase_question": "", "supports_question_id": "",
                "blueprint_slot_id": "仅当输入有该字段时原样返回",
                "recall_candidates": [{
                    "candidate_id": 0, "recall_rank": 1, "answer_role": "result/proof/risk_remove/comfort/scene/styling/trust/mechanism",
                    "purchase_outcome": "", "relevance_reason": "",
                }],
                "selected_candidate_id": 0, "selection_reason": "没有候选时说明原因",
            }],
            "append_purchase_question_route": [{"question_id": "Q3", "question": "", "journey_role": "", "why_now": ""}],
            "append_purchase_cognition_path": [{
                "step_id": next_step_id, "chapter_id": next_chapter_id, "purchase_cognition": "", "purchase_question_id": "Q3",
                "purchase_question": "", "supports_question_id": "", "answer_role": "", "answered_question": "",
                "advance_type": "new_purchase_cognition/necessary_stronger_proof", "candidate_ids": [0], "why_it_advances": "",
            }],
            "append_chapters": [{
                "chapter_id": next_chapter_id, "narrative_role": "new_value", "goal": "", "candidate_ids": [0],
                "asset_tier": "core/supporting/bridge/safe_reserve", "story_support": "", "commerce_beat_id": "",
                "value_dimension": "", "purchase_value_dimension": "new_outcome/same_claim_additional_proof",
                "purchase_value_domain": "", "purchase_value_outcomes": [], "purchase_value_reason": "",
            }],
            "story_consumption": {"hero_strategy_id": "", "hero_priority": "", "hero_consistency_reason": "", "supporting_chapter_ids": [], "bridge_chapter_ids": [], "no_rediscovery": True, "supporting_candidate_ids": [], "bridge_candidate_ids": []},
            "journey_status": "journey_incomplete/source_material_insufficient", "stop_reason": "",
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _as_mapping_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = (value,)
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _parse_purchase_journey_targeted_recall(
    data: Mapping[str, Any], *, missing_questions: Sequence[Mapping[str, Any]],
    unconsumed_tags: Sequence[CommerceLiteTag],
) -> tuple[dict[str, Any], tuple[StrongClipRank, ...], list[str]]:
    """Validate model-declared recall/selection without choosing candidates."""
    missing_by_id = {
        str(item.get("purchase_question_id") or ""): dict(item)
        for item in missing_questions
    }
    tag_ids = {tag.candidate_id for tag in unconsumed_tags}
    errors: list[str] = []
    recalls: list[dict[str, Any]] = []
    selected_ranks: list[StrongClipRank] = []
    selected_ids: set[int] = set()
    seen_questions: set[str] = set()
    for raw_row in _as_mapping_list(data.get("recall_by_question")):
        question_id = _text(raw_row.get("purchase_question_id"))
        spec = missing_by_id.get(question_id)
        if not spec or question_id in seen_questions:
            errors.append(f"targeted_recall_question_invalid_or_repeated:{question_id or 'missing'}")
            continue
        seen_questions.add(question_id)
        question = _text(raw_row.get("purchase_question"))
        if question != _text(spec.get("purchase_question")):
            errors.append(f"targeted_recall_question_not_formal:{question_id}")
        supports_question_id = _text(raw_row.get("supports_question_id"))
        if supports_question_id != _text(spec.get("supports_question_id")):
            errors.append(f"targeted_recall_support_not_formal:{question_id}")
        candidates: list[dict[str, Any]] = []
        seen_candidate_ids: set[int] = set()
        for index, raw_candidate in enumerate(_as_mapping_list(raw_row.get("recall_candidates")), 1):
            ids = _as_int_tuple(raw_candidate.get("candidate_id"))
            candidate_id = ids[0] if ids else 0
            answer_role = _text(raw_candidate.get("answer_role")).lower()
            outcome = _text(raw_candidate.get("purchase_outcome"))
            if candidate_id not in tag_ids or candidate_id in seen_candidate_ids:
                errors.append(f"targeted_recall_candidate_invalid:{question_id}:{candidate_id}")
                continue
            if index > MAX_TARGETED_RECALL_PER_QUESTION:
                errors.append(f"targeted_recall_candidate_cap_exceeded:{question_id}")
                continue
            if answer_role not in set(spec.get("allowed_answer_roles") or ()):
                errors.append(f"targeted_recall_answer_role_invalid:{question_id}:{candidate_id}:{answer_role or 'missing'}")
                continue
            if not outcome:
                errors.append(f"targeted_recall_outcome_missing:{question_id}:{candidate_id}")
                continue
            seen_candidate_ids.add(candidate_id)
            candidates.append({
                "candidate_id": candidate_id,
                "recall_rank": max(1, int(_number(raw_candidate.get("recall_rank")) or index)),
                "answer_role": answer_role,
                "purchase_outcome": outcome,
                "relevance_reason": _text(raw_candidate.get("relevance_reason")),
            })
        selected_values = _as_int_tuple(raw_row.get("selected_candidate_id"))
        selected_candidate_id = selected_values[0] if selected_values else 0
        candidate_by_id = {item["candidate_id"]: item for item in candidates}
        if selected_candidate_id and selected_candidate_id not in candidate_by_id:
            errors.append(f"targeted_recall_selected_not_recalled:{question_id}:{selected_candidate_id}")
        if selected_candidate_id in selected_ids:
            errors.append(f"targeted_recall_selected_repeated:{selected_candidate_id}")
        if candidates and not selected_candidate_id:
            errors.append(f"targeted_recall_new_value_not_selected:{question_id}")
        if selected_candidate_id:
            selected_ids.add(selected_candidate_id)
            selected = candidate_by_id.get(selected_candidate_id)
            if selected:
                selected_ranks.append(StrongClipRank(
                    candidate_id=selected_candidate_id, rank=10_000 + len(selected_ranks),
                    standalone_strength=0.0, hook_power=0.0,
                    purchase_value=_text(raw_row.get("selection_reason")),
                    purchase_outcome=selected["purchase_outcome"], purchase_question_id=question_id,
                    purchase_question=question, supports_question_id=supports_question_id,
                    answer_role=selected["answer_role"], purchase_question_role=selected["answer_role"],
                    answered_question=question, evidence_function=selected["answer_role"],
                    proof_strength=0.0, redundancy_group=f"targeted_{question_id}", fragment=False,
                    visual_dependency=False, opening_rank=0, opening_reason="",
                    selection_reason=selected["relevance_reason"],
                ))
        recalls.append({
            "purchase_question_id": question_id, "purchase_question": question,
            "supports_question_id": supports_question_id, "recall_candidates": candidates,
            "selected_candidate_id": selected_candidate_id,
            "selection_reason": _text(raw_row.get("selection_reason")),
            **({
                "blueprint_slot_id": _text(spec.get("slot_id")),
                "blueprint_priority": int(_number(spec.get("priority")) or 0),
            } if _text(spec.get("slot_id")) else {}),
        })
    for question_id in missing_by_id:
        if question_id not in seen_questions:
            errors.append(f"targeted_recall_question_missing:{question_id}")
    return {
        "missing_purchase_questions": _recall_question_payload(missing_questions),
        "recall_by_question": recalls,
        "journey_status": _text(data.get("journey_status")).lower(),
        "stop_reason": _text(data.get("stop_reason")),
        "selected_candidate_ids": sorted(selected_ids),
    }, tuple(selected_ranks), list(dict.fromkeys(errors))


def _merge_purchase_journey_continuation(
    initial: Mapping[str, Any], continuation: Mapping[str, Any], *, selected_ids: set[int],
) -> tuple[dict[str, Any], list[str]]:
    """Merge only M2-selected append rows; no local candidate choice occurs."""
    merged = dict(initial)
    errors: list[str] = []
    append_route = _as_mapping_list(continuation.get("append_purchase_question_route"))
    append_path = _as_mapping_list(continuation.get("append_purchase_cognition_path"))
    append_chapters = _as_mapping_list(continuation.get("append_chapters"))
    initial_chapter_ids = {
        _text(row.get("chapter_id")) for row in _as_mapping_list(initial.get("chapters"))
        if _text(row.get("chapter_id"))
    }
    append_chapter_ids = [_text(row.get("chapter_id")) for row in append_chapters]
    append_path_chapter_ids = [_text(row.get("chapter_id")) for row in append_path]
    if len(append_route) != len(selected_ids) or len(append_path) != len(selected_ids) or len(append_chapters) != len(selected_ids):
        errors.append("targeted_recall_append_row_count_mismatch")
    if (
        any(not chapter_id for chapter_id in append_chapter_ids)
        or len(set(append_chapter_ids)) != len(append_chapter_ids)
        or set(append_chapter_ids).intersection(initial_chapter_ids)
        or set(append_path_chapter_ids).intersection(initial_chapter_ids)
    ):
        errors.append("targeted_recall_append_chapter_id_reused_or_invalid")
    by_question = {row.get("purchase_question_id"): row for row in append_path}
    by_chapter = {row.get("chapter_id"): row for row in append_chapters}
    for question_id, row in by_question.items():
        candidate_ids = set(_as_int_tuple(row.get("candidate_ids")))
        if len(candidate_ids) != 1 or not candidate_ids.issubset(selected_ids):
            errors.append(f"targeted_recall_append_candidate_invalid:{question_id}")
        if row.get("chapter_id") not in by_chapter:
            errors.append(f"targeted_recall_append_chapter_missing:{question_id}")
        elif tuple(_as_int_tuple(by_chapter[row.get("chapter_id")].get("candidate_ids"))) != tuple(_as_int_tuple(row.get("candidate_ids"))):
            errors.append(f"targeted_recall_append_chapter_candidate_mismatch:{question_id}")
    merged["purchase_question_route"] = _as_mapping_list(initial.get("purchase_question_route")) + append_route
    merged["purchase_cognition_path"] = _as_mapping_list(initial.get("purchase_cognition_path")) + append_path
    merged["chapters"] = _as_mapping_list(initial.get("chapters")) + append_chapters
    if isinstance(continuation.get("story_consumption"), Mapping):
        merged["story_consumption"] = dict(continuation["story_consumption"])
    merged["duration_assessment"] = {
        "status": _text(continuation.get("journey_status")) or "journey_incomplete",
        "reason": _text(continuation.get("stop_reason")),
    }
    return merged, list(dict.fromkeys(errors))


def build_purchase_journey_quality_prompt(
    *, strategy: Any, director_strategy_contract: Mapping[str, Any] | None,
    completed_path_audit: Mapping[str, Any], completed_plan_data: Mapping[str, Any],
    tags: Sequence[CommerceLiteTag],
) -> str:
    """Ask the existing M2 director to polish a completed journey, not extend it."""
    question_ids = list(dict.fromkeys(
        _text(row.get("purchase_question_id"))
        for row in completed_path_audit.get("steps") or ()
        if isinstance(row, Mapping) and _text(row.get("purchase_question_id"))
    ))
    question_payload = _purchase_journey_question_payload(question_ids)
    return "\n".join((
        "你仍在同一个 M2 Purchase Journey 内，现在做的是 Purchase Journey Quality，不是补时长、不是发现新购买问题、不是 M3 改写。",
        "购买旅程补全已经完成。你只能优化当前已经决定要讲的购买问题：每题从完整 executable-safe 原字幕池中做局部 Ranking，先选一条最强锚点原话；只有确有必要时，才可由你再选最多两条不同证据角色的原话，把同一购买问题做成一个能看懂的小段落。绝不能新增问题、绝不能为覆盖 Q1-Q7 或 60 秒凑句。",
        "Q1 和 Q2 是核心，必须保留；Q2 的 mechanism 必须在 Q1 结果之后，并 supports_question_id=Q1。Q3-Q7 是可删除的：如果没有一条足够干净、具体、有说服力的口播，删除该问题比硬塞更好。Q7 尤其如此。",
        "对每题列出最多 5 条局部候选并排序。selected_candidate_ids 的第一条必须是 local_rank 最靠前且 final_utterance_eligible=true 的强锚点；后面可由你添加 0–2 条必要 support，但每条必须使用不同 answer_role、回答同一购买问题、带来新的展示/证明/顾虑解除价值，不能同义重复。未通过口播洁净度的候选可以保留在列表里供审计，但绝不能进入 selected_candidate_ids。程序不替你挑句，只校验你声明的关系、候选来源、质量、不同证据角色和真实时长。",
        "每条候选必须逐项判断：commercial_impact（商业冲击力）、independent_completeness（独立完整度）、specificity（具体程度）、asr_quality（ASR/数字/单位是否可信）、semantic_cleanliness（人听起来是否自然且没有明显怪词、残句、直播接话）、previous_connection（与前文承接度），均为 1-5。",
        "final_utterance_eligible=true 只允许给 asr_quality、semantic_cleanliness、independent_completeness 都至少 4 的句子。不要猜测或静默修正 ASR：明显异常的数字/单位、无意义名词、直播残留起手、未完成句，一律判为不适合最终口播。",
        "本次焦糖验收采用严格口播规则：含有“好的，来……”“像100斤葡萄”“好像往里挖了是那个35厘米”“A类母婴店”这类明显直播残留或 ASR 怪词的原句，必须给 asr_quality 或 semantic_cleanliness 至多 2，且 final_utterance_eligible=false；不能因为其商业结果很强而放行，更不能自行脑补改成正确的话。以“它其实”等依赖前文的开头也不能做 Q1 Opening。",
        "Q1 Opening 还不得以“好的”“来”“的确”“你看”“它其实”等直播接话或承接词开头；若完整候选池中有干净的整体效果原句，必须纳入 Q1 局部比较后再选，不得只在旧核心候选里挑次优句。",
        "开场还必须保留既有 hook_eligible=true 合同；但 hook_eligible 只是权限，不等于好 Hook。Q1 的 selected_candidate_ids 第一条（anchor）必须是 hook_eligible=true，且是当前所有候选中最强、最干净、对陌生用户独立成立的一句。Q1 如需 support，support 可以不是 hook，但必须干净、回答 Q1，并在 anchor 后以 proof 补强；冻结字段 hook_eligible=false 的候选绝不能作为 Q1 anchor 或 Opening。",
        "最后由你重排保留下来的问题，让购买逻辑自然：结果→机制→适穿→夏季体验→风险解除→日常使用通常比突然插入弱品质话术自然。不得让 mechanism 跑到它支持的结果之前；本次成交片若保留 Q5/Q6，Q5 必须先于 Q6。",
        "Q7 只有 commercial_impact=5、independent_completeness/asr_quality/semantic_cleanliness 都至少 4，且能自然接在 Q6 后时才可保留；否则必须删除。删除 Q7 时 selected_candidate_ids=[] 且 selected_candidate_id=0，即便其 local_candidates 里有看似可用的句子也不能选择。",
        "JSON 一致性是硬约束：retained_question_ids 中的每题 selected_candidate_ids 必须以 local_rank=1 的 anchor 开头，最多 3 条且 answer_role 不重复；selected_candidate_id 必须精确等于 anchor。dropped_optional_question_ids 中的每题 selected_candidate_ids 必须为空、selected_candidate_id 必须为 0，且绝不能出现在 final_order；final_order 必须恰好等于 retained_question_ids 的顺序，并用 candidate_ids 精确保留该题的锚点和 support 顺序。",
        "不得改写字幕、不得拼接候选、不得复用 candidate_id、不得从候选池外引用 candidate_id。",
        "M1 固定故事事实：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前导演方案：",
        json.dumps(dict(director_strategy_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "本轮允许保留或删除、但绝不能新增的正式购买问题：",
        json.dumps(question_payload, ensure_ascii=False, separators=(",", ":")),
        "已完成的购买问题集合仅用于限定本轮范围；此前选中的 candidate、顺序和口播一律不是优先项，必须从完整池重新比较：",
        json.dumps({"covered_question_ids": question_ids}, ensure_ascii=False, separators=(",", ":")),
        "完整 executable-safe candidate pool：",
        json.dumps([tag.to_dict() for tag in tags if tag.materializable], ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "quality_by_question": [{
                "purchase_question_id": "Q1", "purchase_question": "", "supports_question_id": "",
                "local_candidates": [{
                    "candidate_id": 0, "local_rank": 1, "purchase_outcome": "", "answer_role": "result/mechanism/proof/risk_remove/comfort/scene/styling/trust",
                    "commercial_impact": 1, "independent_completeness": 1, "specificity": 1,
                    "asr_quality": 1, "semantic_cleanliness": 1, "previous_connection": 1,
                    "final_utterance_eligible": False, "quality_reason": "",
                }],
                "selected_candidate_ids": [0], "selected_candidate_id": 0, "omit_reason": "非核心问题可删除；Q1/Q2 不可删除",
            }],
            "retained_question_ids": ["Q1", "Q2"],
            "dropped_optional_question_ids": ["Q7"],
            "final_order": [{"question_id": "Q1", "candidate_ids": [0], "narrative_role": "hook", "purchase_value_domain": "", "purchase_value_reason": "", "purchase_cognition": "", "why_now": "", "why_it_advances": ""}],
            "opening_package": {"hook_promise": "", "payoff_delivery": "", "connection_reason": "", "hook_candidate_ids": [0], "payoff_candidate_ids": [0]},
            "story_consumption": {"hero_strategy_id": _text(getattr(strategy, "strategy_id", "")), "hero_priority": _text(getattr(strategy, "story_priority", "")), "hero_consistency_reason": "", "no_rediscovery": True},
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def build_purchase_question_local_quality_prompt(
    *, question: Mapping[str, Any], tags: Sequence[CommerceLiteTag],
    existing_answer_anchor: Mapping[str, Any] | None = None,
    excluded_candidate_ids: Sequence[int] = (),
    narrative_depth: Mapping[str, Any] | None = None,
    early_journey_scope: Mapping[str, Any] | None = None,
    retry_reason: str = "",
) -> str:
    """A focused, full-pool M2 comparison for exactly one buyer question."""
    question_id = _text(question.get("purchase_question_id"))
    scope = _director_early_journey_scope({"early_journey_scope": early_journey_scope}) if isinstance(early_journey_scope, Mapping) else _director_early_journey_scope(None)
    opening_question_id = _text((scope.get("opening_question_ids") or ("",))[0])
    opening_scope = dict((narrative_depth or {}).get("opening_scope") or {})
    opening_roles = {
        _text(item).lower() for item in opening_scope.get("allowed_answer_roles") or () if _text(item)
    }
    # Only the Q1 anchor has an opening-permission requirement.  A support
    # line in the same Q1 micro-sequence is still selected by M2 from the
    # full pool and may be hook-ineligible; code never chooses that line.
    consumed_ids = {int(item) for item in excluded_candidate_ids if int(item) > 0}
    candidate_pool = [
        tag for tag in tags
        if tag.materializable and tag.candidate_id not in consumed_ids
    ]
    conditional_opening_rule = (
        "本题是当前 Archetype 的 Opening anchor：selected_candidate_ids 第一条必须 hook_eligible=true、"
        "commercial_impact、independent_completeness、specificity、asr_quality、semantic_cleanliness 都至少为4，"
        "且 answer_role 必须属于 opening_scope 的允许角色 " + json.dumps(sorted(opening_roles), ensure_ascii=False) + "。它必须是干净、独立的最终口播；范围外的全局高分句不得替代它。"
        "support 不需要 hook_eligible，但必须紧跟 anchor、同样干净、真正回答本题；不得为了凑时长改选更长但不干净的句子。"
        if question_id == opening_question_id else
        "本题不是当前 Archetype 的 Opening anchor：不要把 hook_eligible、开场时长或开场接话规则套到本题；只按本题的口播干净度和购买价值判断。"
    )
    depth_slot = next((
        dict(slot) for slot in (narrative_depth or {}).get("chapter_slots") or ()
        if isinstance(slot, Mapping) and _text(slot.get("purchase_question_id")) == question_id
    ), None)
    depth_instruction = (
        "本片处于 45–75 秒 Narrative Depth。当前 Director Blueprint slot 为："
        + json.dumps(depth_slot, ensure_ascii=False, separators=(",", ":"))
        + "。它是高优先级购买章节，不是时长指标。若完整池有干净、独立、符合该 slot answer_roles 的新购买价值，必须把它列入局部比较；最终是否加入仍由你选。"
        if depth_slot else ""
    )
    depth_q1_sequence_rule = (
        "本片处于 Narrative Depth，Q1 只保留一个强结果 anchor：selected_candidate_ids 必须恰好只有这一条。"
        "不要在 Q1 追加第二个 result 或尺码 proof；Q2 必须独立承接机制，Q3 必须独立承接身材/尺码适穿。"
        "这不是删减时长，而是防止把后续的新购买章节伪装成 Q1 support。"
        # This owns the result/fit boundary even when another Archetype (for
        # example scene immersion) deliberately opens with Q6/Q4 rather than
        # Q1.  The director still selects the one Q1 result anchor.
        if narrative_depth is not None and question_id == "Q1" else ""
    )
    retry_instruction = (
        "上一版本题局部选择违反了程序关系合同，必须只重做【本题】的 local_candidates 和 selected_candidate_ids；"
        "不能换题、不能把别题候选挪进来、不能删掉本题本身。错误如下："
        + _text(retry_reason)
        + "。你仍是唯一决定语义候选的人；程序只会验证你重答的角色、干净度和不重复关系。"
        if retry_reason else ""
    )
    return "\n".join((
        "你在 M2 内为一个购买问题做局部 Strong Clip Ranking。不是写脚本、不是补时长；只能从完整 executable-safe 原字幕池选句。",
        "逐条按 commercial_impact、independent_completeness、specificity、asr_quality、semantic_cleanliness、previous_connection 打 1-5 分；列出最多 5 个局部候选，按 local_rank 排列。另必须逐条给出 spoken_completeness=complete/dependent/incomplete：complete 只指剪出来可自然开头、自然收尾、陌生人无需补前文也能听完整的一句话；dependent/incomplete 包括“因为它…/所以这个…/你想要在…/会显得我们的肩干嘛？”等承接残句、未回答反问、无来源指代。",
        "selected_candidate_ids 第一条必须是 local_rank 最靠前、且 asr_quality/semantic_cleanliness/independent_completeness 均至少4、spoken_completeness=complete 的强锚点；你最多可再选 1 条必要 support。support 可以和锚点同 answer_role，但同一 purchase_question 下 answer_role+purchase_outcome 不得重复。support 必须同样干净、spoken_completeness=complete，并明确 incremental_purchase_value=true 与理由：删掉它后，观众必须会失去一个锚点没有的购买决定，而不只是少听一次同义强调、更多细节或另一种说法。它若主要回答另一个已保留 Q（例如夏季舒适），必须留给那个 Q，不能借本题凑时长。若没有必要 support，就只选锚点。selected_candidate_id 必须等于 selected_candidate_ids 第一条。若该问题非核心且没有合格锚点，selected_candidate_ids=[]、selected_candidate_id=0。不得猜测或改正 ASR。",
        "candidate pool 中 final_utterance_selection_status=blocked 的候选仍列出供审计，但它是已知原句异常/直播互动残句：final_utterance_eligible 必须为 false、asr_quality 或 semantic_cleanliness 至多为2，且绝不能进入 selected_candidate_ids。status=selectable 只代表允许比较，不代表必须选它。",
        "把它当作最终口播逐字试听：出现直播接话、残句、明显怪词、异常数字/单位或需要观众替它脑补时，final_utterance_eligible 必须为 false，不能因购买逻辑正确而放行。尤其不能把“35厘米”这类疑似 ASR 数字错误当作干净机制句。",
        "焦糖本轮的明确反例是“好的，来一拉开160斤，一收上看起来像100斤葡萄”“好像往里挖了是那个35厘米”“木浆纤维可降解的A类母婴店，就是你小宝宝”“你会觉得这个人间一定是直角的”“大斜方间了”“上身完全不”。它们都不是可用的最终口播；不得复述、改正或以高分放行。",
        "焦糖已审计的同题冗余基准：57“全松紧带。里衬全弹的，你都不用穿打底裤了。”已经完整回答弹力、里衬和不用打底裤；280“它全弹力的嘛，而且我们还做打底裤嘞。”没有新增购买认知。Q5 比较到这两句时，280 必须 incremental_purchase_value=false 且不得选为 support。",
        "所有问题都适用独立口播规则：以“有没有觉得”“是不是”“你看”“看到了吗”发起直播互动，或只说“从这儿到这儿”等必须看手势才懂的指点式表达，semantic_cleanliness 至多 2、final_utterance_eligible=false。Q2 应优先选择能用完整语言讲清版型/花边线机制的句子，而不是靠现场指点的句子。",
        "Q1 的原句必须直接说出一个陌生观众能理解的、可见的购买结果（例如显瘦、显窄、肩变窄、腰胯修饰或明确大码效果）；“有修饰作用”之类没有具体结果的泛话、需要靠上下文补全的句子，不得作为 Q1 或 Opening，即使 hook_eligible=true。",
        "Q3 已在 Q1 结果、Q2 机制之后时，体重尺码对照表通常是 proof，不是 result；若完整池同时有一句清楚说出大身材能穿、并说明肩/腰/胯整体效果的原话，它应作为 result anchor，尺码表可作为 proof support。两句都干净且各自回答“效果/适穿依据”时，应选成一个 result→proof 微段落；不能因为 result 句不能独立开场就把它错判为不可用，也不能把两个尺码重复句当作微段落。",
        "Q4 若完整池同时存在“料子透薄/三伏天能穿”与“不粘身/凉爽”的干净原话，前者可作为 proof（面料/季节依据）排为 anchor，后者可作为 comfort（真实体感）排为 support；两句各自带来依据与体验时，应选成一个 proof→comfort 微段落。若两句只是同义重复，则只留锚点。",
        "每一个 local_candidate 都必须真正回答【这一个】购买问题；answer_role 只能使用正式表的 allowed_answer_roles。不得把另一个问题（例如面料信任）伪装成当前问题（例如显瘦机制），也不得因为它商业信息强就列入。",
        "Q4 只接受用户能直接感受到的正向夏季体验，例如透气、不粘肉、三伏天、凉爽；单纯说领口大、可能透、版型宽松，不能单独回答“夏天舒服吗”，必须降分或不选。",
        depth_instruction,
        depth_q1_sequence_rule,
        retry_instruction,
        conditional_opening_rule,
        "Q2 必须 supports_question_id=Q1；其他问题 supports_question_id 必须严格使用正式表。",
        "正式购买问题：",
        json.dumps(dict(question), ensure_ascii=False, separators=(",", ":")),
        "本题已由 M2 初稿确定的购买方向（不是指定候选；只能由更强、更干净、同一购买价值的原话替换，不能换成别的卖点）：",
        json.dumps(dict(existing_answer_anchor or {}), ensure_ascii=False, separators=(",", ":")),
        "本题可比较 candidate pool（Q1 anchor 必须遵守既有 Opening 硬合同；其他选择均来自完整 executable-safe pool）：",
        json.dumps([_quality_candidate_pool_item(tag) for tag in candidate_pool], ensure_ascii=False, separators=(",", ":")),
        ("此前已被前序购买问题消费、不可重复使用的 candidate_id：" + json.dumps(sorted(consumed_ids))) if consumed_ids else "",
        "只返回 JSON：",
        json.dumps({
            "purchase_question_id": question_id, "purchase_question": _text(question.get("purchase_question")),
            "supports_question_id": _text(question.get("supports_question_id")),
            "local_candidates": [{
                "candidate_id": 0, "local_rank": 1, "purchase_outcome": "", "answer_role": "result/mechanism/proof/risk_remove/comfort/scene/styling/trust",
                "commercial_impact": 1, "independent_completeness": 1, "specificity": 1,
                "asr_quality": 1, "semantic_cleanliness": 1, "previous_connection": 1,
                "spoken_completeness": "complete/dependent/incomplete", "spoken_completeness_reason": "",
                "incremental_purchase_value": False, "incremental_purchase_value_reason": "",
                "final_utterance_eligible": False, "quality_reason": "",
            }],
            "selected_candidate_ids": [0], "selected_candidate_id": 0, "omit_reason": "",
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def build_purchase_journey_quality_order_prompt(
    *, strategy: Any, local_quality_rows: Sequence[Mapping[str, Any]],
    required_question_ids: Sequence[str] = (), retry_reason: str = "",
    narrative_depth: Mapping[str, Any] | None = None,
    early_journey_scope: Mapping[str, Any] | None = None,
    candidate_text_by_id: Mapping[int, Any] | None = None,
) -> str:
    """Have M2 keep/delete and order already locally ranked question answers."""
    required_ids = list(dict.fromkeys(_text(item) for item in required_question_ids if _text(item)))
    scope = _director_early_journey_scope({"early_journey_scope": early_journey_scope}) if isinstance(early_journey_scope, Mapping) else _director_early_journey_scope(None)
    opening_question_ids = list(scope["opening_question_ids"])
    retry_instruction = (
        "上一版排序不合格，原因如下。不要重新选择候选、不要新加购买问题；只在现有局部 selected_candidate_ids 小段落上纠正保留集合与顺序。\n"
        + _text(retry_reason)
        if retry_reason else ""
    )
    blueprint_order = [
        _text(slot.get("purchase_question_id"))
        for slot in (narrative_depth or {}).get("chapter_slots") or ()
        if isinstance(slot, Mapping) and _text(slot.get("purchase_question_id"))
    ]
    depth_instruction = (
        "本片处于 45–75 秒 Narrative Depth。开场路径后，必须优先遵循 Director Blueprint 的章节顺序："
        + "→".join(blueprint_order)
        + "。这不是固定秒数；只有当前题没有干净新购买价值时才可删除。若 clean anchor 已存在，不能为了缩短而删除 Blueprint 高价值章节。"
        if blueprint_order else ""
    )
    return "\n".join((
        "你仍在同一个 M2 Purchase Journey Quality 内。每个购买问题的局部候选、强锚点和必要 support 已由 M2 完成；你现在只有删除非核心问题、删掉无新增认知的同题 support、以及重排保留问题的导演权，不能替换 candidate、不能新增问题、不能为了时长加句。每个保留问题的 candidate_ids 必须以局部 selected_candidate_ids 的 anchor 为第一条，随后只能保留原序 support 的子序列；不得改选或新增 ID。",
        "前两题必须严格为当前 Archetype opening_question_ids，且分别作为 hook/payoff；required 问题必须保留。recommended 仅在有干净独立新购买价值时保留；optional 可以主动删除。Q7 只有商业冲击力为5且自然收束时可保留，否则删除。",
        "当前 Archetype early_journey_scope：" + json.dumps(scope, ensure_ascii=False, separators=(",", ":")),
        depth_instruction,
        "删除权只针对弱问题：只要 recommended 问题的 selected_candidate_ids 非空，且 anchor final_utterance_eligible=true、commercial_impact/independent_completeness/asr_quality/semantic_cleanliness 都至少为4，就必须保留；不得为了把片子变短而删除已经证明有独立新购买价值的问题。optional 问题则可删除。",
        ("本次必须保留的问题：" + json.dumps(required_ids, ensure_ascii=False, separators=(",", ":"))) if required_ids else "",
        retry_instruction,
        "同题最终最多保留 anchor + 1 条必要 support。support 只有在删掉后会失去一个锚点没有、且不由其他保留 Q 覆盖的购买决定时才保留；若第二句只是重复同一弹力/里衬/打底裤、同一机制或同一结果，或实际是在回答另一个保留 Q（例如把夏季舒适塞进 Q5），必须从该题 candidate_ids 删除。不得因为它被标作 proof 就保留重复。跨题也一样：一个 support 若主要回答另一个将保留的问题，必须从当前题删除、留给它自己的问题。例如 Q3 保留尺码适穿时，不得再把同一尺码表塞进 Q1 的显瘦结果微段落。dropped_optional_question_ids 中的问题不出现在 final_order。opening_package 的 hook_candidate_ids 必须精确等于 opening_question_ids[0] 的最终 candidate_ids；其中第一条才是唯一 Hook anchor，后续只可为必要 support。payoff_candidate_ids 必须精确等于 opening_question_ids[1] 的最终 candidate_ids；不得把后续问题写成 payoff。若 hook_candidate_ids 的真实总时长超过 5 秒，hook_integrity_reason 必须具体说明为什么其中每句共同构成当前承诺、删掉会损失什么；5 秒以内留空。",
        "逐行实际连读上一句→当前句→下一句的原话。每个 final_order 行必须填写 continuity_assessment：previous_to_current、current_to_next 只能是 natural/awkward/natural_end，及 reason。保留行不得出现 awkward；当前句若话题突跳、重复上一句、指代没来源、承接残句或像直播硬拼，必须删除弱题/重复 support 或重排。",
        "每个 final_order 行要说明 purchase_cognition、why_now、why_it_advances、purchase_value_domain、purchase_value_reason；不得写入并不存在于原字幕的事实。",
        "M1 固定故事：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "M2 已完成的局部质量排序：",
        json.dumps([dict(row) for row in local_quality_rows], ensure_ascii=False, separators=(",", ":")),
        "这些 candidate_id 的真实原话与时长（只能据此判断连读和 Hook 软容忍理由；不得改写）：",
        json.dumps({str(key): value for key, value in (candidate_text_by_id or {}).items()}, ensure_ascii=False, separators=(",", ":")),
        "只返回 JSON：",
        json.dumps({
            "retained_question_ids": opening_question_ids, "dropped_optional_question_ids": ["Q7"],
            "final_order": [{"question_id": opening_question_ids[0], "candidate_ids": [0], "narrative_role": "hook", "purchase_value_domain": "", "purchase_value_reason": "", "purchase_cognition": "", "why_now": "", "why_it_advances": "", "continuity_assessment": {"previous_to_current": "natural", "current_to_next": "natural", "reason": ""}}],
            "opening_package": {"hook_promise": "", "payoff_delivery": "", "connection_reason": "", "hook_integrity_reason": "", "hook_candidate_ids": [0], "payoff_candidate_ids": [0]},
            "story_consumption": {"hero_strategy_id": _text(getattr(strategy, "strategy_id", "")), "hero_priority": _text(getattr(strategy, "story_priority", "")), "hero_consistency_reason": "", "no_rediscovery": True},
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _strong_optional_quality_question_ids(
    rows: Sequence[Mapping[str, Any]], *, required_question_ids: Sequence[str] = (),
    recommended_question_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return strong Q3-Q6 answers that a quality order may not discard.

    This deliberately reads only the director's own local-ranking declaration.
    It does not infer candidate meaning or select a replacement; it prevents a
    later ordering response from contradicting an already declared clean,
    independent answer to a new buyer question.
    """
    required_ids = {_text(item) for item in required_question_ids if _text(item)}
    recommended_ids = {_text(item) for item in recommended_question_ids if _text(item)}
    strong: list[str] = []
    for row in rows:
        question_id = _text(row.get("purchase_question_id"))
        if question_id not in ({"Q3", "Q4", "Q5", "Q6"} | required_ids | recommended_ids):
            continue
        selected_values = _quality_selected_candidate_ids(row)
        selected_id = selected_values[0] if selected_values else 0
        if not selected_id:
            continue
        selected = next((
            item for item in _as_mapping_list(row.get("local_candidates"))
            if (_as_int_tuple(item.get("candidate_id")) or (0,))[0] == selected_id
        ), None)
        if not isinstance(selected, Mapping) or not _as_bool(selected.get("final_utterance_eligible")):
            continue
        if min(
            _number(selected.get("commercial_impact")),
            _number(selected.get("independent_completeness")),
            _number(selected.get("asr_quality")),
            _number(selected.get("semantic_cleanliness")),
        ) >= 4:
            strong.append(question_id)
    return tuple(dict.fromkeys(strong))


def _quality_order_opening_contract_errors(
    order_data: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
    *, opening_question_ids: Sequence[str] = ("Q1", "Q2"),
    selected_by_question_override: Mapping[str, Sequence[int]] | None = None,
    tags: Sequence[CommerceLiteTag] = (),
) -> tuple[str, ...]:
    """Validate Archetype-declared opening IDs without selecting any content."""
    selected_by_question: dict[str, tuple[int, ...]] = {
        _text(question_id): tuple(int(item) for item in values)
        for question_id, values in (selected_by_question_override or {}).items()
        if _text(question_id) and values
    }
    if not selected_by_question:
        for row in rows:
            question_id = _text(row.get("purchase_question_id"))
            values = _quality_selected_candidate_ids(row)
            if question_id and values:
                selected_by_question[question_id] = values
    questions = tuple(_text(item) for item in opening_question_ids if _text(item))
    if len(questions) != 2:
        return ("purchase_quality_opening_scope_invalid",)
    expected_hook = selected_by_question.get(questions[0], ())
    expected_payoff = selected_by_question.get(questions[1], ())
    opening = order_data.get("opening_package")
    if not isinstance(opening, Mapping):
        legacy_plan = order_data.get("final_plan")
        opening = legacy_plan.get("opening_package") if isinstance(legacy_plan, Mapping) else None
    if not isinstance(opening, Mapping) or not expected_hook or not expected_payoff:
        return ("purchase_quality_opening_package_missing_or_core_unselected",)
    hook_ids = _as_int_tuple(opening.get("hook_candidate_ids"))
    payoff_ids = _as_int_tuple(opening.get("payoff_candidate_ids"))
    errors: list[str] = []
    if set(expected_hook).intersection(expected_payoff):
        errors.append(
            "purchase_quality_opening_q1_q2_candidate_reused"
            if questions == ("Q1", "Q2") else "purchase_quality_opening_candidate_reused"
        )
    if hook_ids != expected_hook:
        errors.append(
            "purchase_quality_opening_hook_must_be_q1"
            if questions == ("Q1", "Q2") else "purchase_quality_opening_hook_not_archetype_scope"
        )
    if payoff_ids != expected_payoff:
        errors.append(
            "purchase_quality_opening_payoff_must_be_q2"
            if questions == ("Q1", "Q2") else "purchase_quality_opening_payoff_not_archetype_scope"
        )
    # The common narrative validator accepts a 5–8s Hook only with the
    # director's own integrity rationale.  This only requests the same M2
    # ordering response to explain its own retained sequence; code neither
    # trims nor selects an utterance here.
    duration_by_id = {tag.candidate_id: float(tag.duration) for tag in tags if tag.materializable}
    hook_duration = sum(duration_by_id.get(candidate_id, 0.0) for candidate_id in hook_ids)
    if hook_duration > HOOK_PREFERRED_MAX_SECONDS and not _text(opening.get("hook_integrity_reason")):
        errors.append("purchase_quality_opening_hook_integrity_reason_missing")
    return tuple(errors)


def _quality_order_candidate_sequence_contract(
    order_data: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[int, ...]], tuple[str, ...]]:
    """Allow M2 to drop a redundant support without replacing an utterance.

    The ordering decision may only retain the declared anchor and an in-order
    subset of the same local support.  This validates a director deletion; it
    never determines which support is redundant.
    """
    local_by_question = {
        _text(row.get("purchase_question_id")): _quality_selected_candidate_ids(row)
        for row in rows
        if _text(row.get("purchase_question_id"))
    }
    order_rows = _as_mapping_list(order_data.get("final_order"))
    if not order_rows:
        return dict(local_by_question), ()
    selected: dict[str, tuple[int, ...]] = {}
    errors: list[str] = []
    for row in order_rows:
        question_id = _text(row.get("question_id"))
        original = local_by_question.get(question_id, ())
        proposed = _as_int_tuple(row.get("candidate_ids")) or _as_int_tuple(row.get("candidate_id"))
        if not original or not proposed:
            errors.append(f"purchase_quality_final_order_candidate_missing:{question_id or 'missing'}")
            continue
        if proposed[0] != original[0]:
            errors.append(f"purchase_quality_final_order_anchor_replaced:{question_id}")
            continue
        iterator = iter(original)
        if any(candidate_id not in iterator for candidate_id in proposed):
            errors.append(f"purchase_quality_final_order_candidate_not_anchor_preserving_subsequence:{question_id}")
            continue
        selected[question_id] = proposed
    return selected, tuple(dict.fromkeys(errors))


def _quality_order_literal_redundancy_contract_errors(
    order_data: Mapping[str, Any], tags: Sequence[CommerceLiteTag],
) -> tuple[str, ...]:
    """Reject only explicit transcript-fact repetition; never choose a cut.

    This is intentionally narrower than semantic similarity.  It catches an
    ASR line duplicated verbatim (or one line wholly contained in the other),
    and a size-table support whose concrete weight ranges are already a subset
    of its anchor.  The bounded M2 retry decides whether to drop the support
    or keep a different *already selected* subsequence; code cannot add or
    replace an utterance.
    """
    text_by_id = {tag.candidate_id: _text(tag.text) for tag in tags if tag.materializable}
    rows = _as_mapping_list(order_data.get("final_order"))
    selected_rows: list[tuple[str, tuple[int, ...]]] = []
    for row in rows:
        question_id = _text(row.get("question_id"))
        candidate_ids = _as_int_tuple(row.get("candidate_ids")) or _as_int_tuple(row.get("candidate_id"))
        if question_id and candidate_ids:
            selected_rows.append((question_id, candidate_ids))

    def normalized(candidate_id: int) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text_by_id.get(candidate_id, "")).lower()

    def size_ranges(candidate_id: int) -> set[tuple[int, int]]:
        return {
            (int(start), int(end))
            for start, end in re.findall(r"(\d{2,3})\s*(?:到|[-~至])\s*(\d{2,3})\s*斤", text_by_id.get(candidate_id, ""))
        }

    errors: list[str] = []
    for question_id, candidate_ids in selected_rows:
        anchor_id = candidate_ids[0]
        anchor_text = normalized(anchor_id)
        anchor_ranges = size_ranges(anchor_id)
        for support_id in candidate_ids[1:]:
            support_text = normalized(support_id)
            support_ranges = size_ranges(support_id)
            if (
                support_text and anchor_text and (support_text in anchor_text or anchor_text in support_text)
            ) or (support_ranges and support_ranges.issubset(anchor_ranges)):
                errors.append(f"purchase_quality_support_no_new_literal_fact:{question_id}:{support_id}")
    for row_index, (question_id, candidate_ids) in enumerate(selected_rows):
        for support_id in candidate_ids[1:]:
            support_text = normalized(support_id)
            if not support_text:
                continue
            for later_question_id, later_ids in selected_rows[row_index + 1:]:
                later_anchor_id = later_ids[0]
                later_text = normalized(later_anchor_id)
                if support_text and later_text and (support_text in later_text or later_text in support_text):
                    errors.append(
                        "purchase_quality_support_repeats_later_question:"
                        f"{question_id}:{support_id}:{later_question_id}:{later_anchor_id}"
                    )
    return tuple(dict.fromkeys(errors))


def _quality_order_flow_contract_errors(order_data: Mapping[str, Any]) -> tuple[str, ...]:
    """Require the existing M2 ordering pass to audit real three-line flow."""
    order_rows = _as_mapping_list(order_data.get("final_order"))
    if not order_rows:
        # Previous verbose final_plan fixtures remain readable, while every
        # current compact ordering response is required to produce this audit.
        return ()
    errors: list[str] = []
    for index, row in enumerate(order_rows, 1):
        question_id = _text(row.get("question_id")) or str(index)
        flow = row.get("continuity_assessment")
        if not isinstance(flow, Mapping):
            errors.append(f"purchase_quality_continuity_assessment_missing:{question_id}")
            continue
        before = _text(flow.get("previous_to_current")).lower()
        after = _text(flow.get("current_to_next")).lower()
        reason = _text(flow.get("reason"))
        if before not in {"natural", "natural_start"}:
            errors.append(f"purchase_quality_continuity_previous_current_not_natural:{question_id}")
        valid_after = {"natural", "natural_end"} if index == len(order_rows) else {"natural"}
        if after not in valid_after:
            errors.append(f"purchase_quality_continuity_current_next_not_natural:{question_id}")
        if not reason:
            errors.append(f"purchase_quality_continuity_reason_missing:{question_id}")
    return tuple(dict.fromkeys(errors))


def _quality_order_journey_contract_errors(
    order_data: Mapping[str, Any], *, narrative_depth: Mapping[str, Any] | None = None,
    early_journey_scope: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Validate M2's declared buyer-question order without choosing content.

    The director still decides which optional buyer questions merit keeping.
    Once it keeps them, code only rejects an order that breaks the established
    result -> mechanism -> new value -> experience/risk -> styling journey.
    """
    order_rows = _as_mapping_list(order_data.get("final_order"))
    if not order_rows:
        legacy_plan = order_data.get("final_plan")
        if isinstance(legacy_plan, Mapping):
            order_rows = _as_mapping_list(legacy_plan.get("purchase_question_route"))
    question_ids = [_text(row.get("question_id")) for row in order_rows]
    errors: list[str] = []
    has_archetype_scope = isinstance(early_journey_scope, Mapping)
    scope = _director_early_journey_scope({"early_journey_scope": early_journey_scope}) if has_archetype_scope else _director_early_journey_scope(None)
    opening_ids = list(scope["opening_question_ids"])
    if question_ids[:2] != opening_ids:
        errors.append("purchase_quality_journey_order_must_start_archetype_opening_scope")
    legacy_blueprint_order = [
        _text(slot.get("purchase_question_id"))
        for slot in (narrative_depth or {}).get("chapter_slots") or ()
        if isinstance(slot, Mapping) and _text(slot.get("purchase_question_id"))
    ]
    canonical = list(scope["preferred_question_order"]) if has_archetype_scope else (
        ["Q1", "Q2"] + legacy_blueprint_order if legacy_blueprint_order else list(scope["preferred_question_order"])
    )
    if not canonical:
        canonical = ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"]
    canonical_positions = [canonical.index(question_id) for question_id in question_ids if question_id in canonical]
    if canonical_positions != sorted(canonical_positions):
        errors.append("purchase_quality_journey_order_not_purchase_progression")
    if "Q7" in question_ids and question_ids[-1] != "Q7":
        errors.append("purchase_quality_journey_trust_must_only_close")
    # Styling is a closing use-value only in the legacy conversion path.  An
    # Archetype-owned preferred order is the authority for P0.1.
    if "Q6" in question_ids and not has_archetype_scope and not legacy_blueprint_order:
        styling_index = question_ids.index("Q6")
        if not any(question_id in {"Q3", "Q4", "Q5"} for question_id in question_ids[2:styling_index]):
            errors.append("purchase_quality_journey_styling_requires_prior_new_value")
    return tuple(errors)


def _quality_local_selection_contract_errors(
    row: Mapping[str, Any], *, tags: Sequence[CommerceLiteTag],
    required_question_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Validate one M2 local-ranking declaration without selecting a line.

    This is only a relation and sentence-safety receipt.  A bounded retry below
    returns the same buyer question and legal pool to M2 when it fails; code
    never substitutes, removes, or ranks a semantic candidate itself.
    """
    question_id = _text(row.get("purchase_question_id"))
    spec = PURCHASE_JOURNEY_BY_ID.get(question_id)
    if spec is None:
        return (f"purchase_quality_question_invalid_or_repeated:{question_id or 'missing'}",)
    tag_by_id = {tag.candidate_id: tag for tag in tags if tag.materializable}
    allowed_roles = set(spec.get("allowed_answer_roles") or ())
    errors: list[str] = []
    by_id: dict[int, Mapping[str, Any]] = {}
    local_ranks: set[int] = set()
    selected_ids = _quality_selected_candidate_ids(row)
    for index, raw in enumerate(_as_mapping_list(row.get("local_candidates")), 1):
        candidate_id = (_as_int_tuple(raw.get("candidate_id")) or (0,))[0]
        local_rank = max(1, int(_number(raw.get("local_rank")) or index))
        role = _text(raw.get("answer_role")).lower()
        if candidate_id not in tag_by_id or candidate_id in by_id:
            errors.append(f"purchase_quality_candidate_invalid:{question_id}:{candidate_id}")
            continue
        if index > MAX_PURCHASE_QUESTION_QUALITY_CANDIDATES:
            errors.append(f"purchase_quality_candidate_cap_exceeded:{question_id}")
            continue
        if local_rank in local_ranks:
            errors.append(f"purchase_quality_local_rank_repeated:{question_id}:{local_rank}")
            continue
        local_ranks.add(local_rank)
        if role not in allowed_roles:
            # The local ranking may retain a rejected comparison candidate to
            # make its reason visible (for example, a Q2 mechanism listed in
            # Q1 solely to say it is *not* a Q1 result).  It cannot enter the
            # final edit unless selected, so only a selected relation is a
            # hard contract failure.  Code never promotes this alternative.
            if candidate_id in selected_ids:
                errors.append(f"purchase_quality_answer_role_invalid:{question_id}:{candidate_id}:{role or 'missing'}")
            continue
        # Rejected local alternatives remain visible for audit.  They need
        # not invent a formal outcome merely to explain why a malformed ASR
        # fragment is unplayable; an actually selected line is checked below.
        by_id[candidate_id] = raw
    if len(selected_ids) > MAX_PURCHASE_QUESTION_QUALITY_SELECTED_CANDIDATES:
        errors.append(f"purchase_quality_selected_candidate_cap_exceeded:{question_id}")
    if len(selected_ids) != len(set(selected_ids)):
        errors.append(f"purchase_quality_selected_candidate_repeated_in_question:{question_id}")
    required_ids = {_text(item) for item in required_question_ids if _text(item)}
    if not required_ids:
        required_ids = {
            question_key for question_key, question_spec in PURCHASE_JOURNEY_BY_ID.items()
            if bool(question_spec.get("core"))
        }
    if not selected_ids and question_id in required_ids:
        errors.append(f"purchase_quality_core_question_omitted:{question_id}")
    selected_role_outcomes: set[tuple[str, str]] = set()
    for index, candidate_id in enumerate(selected_ids):
        raw = by_id.get(candidate_id)
        if raw is None:
            errors.append(f"purchase_quality_selected_candidate_not_ranked:{question_id}:{candidate_id}")
            continue
        role = _text(raw.get("answer_role")).lower()
        if not _text(raw.get("purchase_outcome")):
            errors.append(f"purchase_quality_outcome_missing:{question_id}:{candidate_id}")
        role_outcome = (role, _text(raw.get("purchase_outcome")))
        if index and role_outcome in selected_role_outcomes:
            errors.append(
                f"purchase_quality_support_role_outcome_repeated:{question_id}:{candidate_id}:{role}:{role_outcome[1]}"
            )
        selected_role_outcomes.add(role_outcome)
        if not _as_bool(raw.get("final_utterance_eligible")) or min(
            _number(raw.get("asr_quality")), _number(raw.get("semantic_cleanliness")),
            _number(raw.get("independent_completeness")),
        ) < 4:
            errors.append(f"purchase_quality_selected_candidate_not_clean:{question_id}:{candidate_id}")
        if _text(raw.get("spoken_completeness")).lower() != "complete":
            errors.append(f"purchase_quality_spoken_completeness_not_complete:{question_id}:{candidate_id}")
        if index and not _as_bool(raw.get("incremental_purchase_value")):
            errors.append(f"purchase_quality_support_not_incremental:{question_id}:{candidate_id}")
        literal_reject = _quality_final_utterance_reject_reason(tag_by_id[candidate_id].text)
        if literal_reject:
            errors.append(f"purchase_quality_known_unplayable_utterance:{question_id}:{candidate_id}:{literal_reject}")
    return tuple(dict.fromkeys(errors))


def _parse_purchase_journey_quality(
    data: Mapping[str, Any], *, current_question_ids: Sequence[str],
    tags: Sequence[CommerceLiteTag], required_question_ids: Sequence[str] = (),
    optional_question_ids: Sequence[str] = (), opening_scope: Mapping[str, Any] | None = None,
    early_journey_scope: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[StrongClipRank, ...], Mapping[str, Any], list[str]]:
    """Validate an AI quality selection without scoring or choosing it locally."""
    question_ids = list(dict.fromkeys(_text(item) for item in current_question_ids if _text(item)))
    required_ids = {_text(item) for item in required_question_ids if _text(item)}
    if not required_ids:
        required_ids = {
            question_key for question_key, question_spec in PURCHASE_JOURNEY_BY_ID.items()
            if bool(question_spec.get("core"))
        }
    early_scope = _director_early_journey_scope({"early_journey_scope": early_journey_scope}) if isinstance(early_journey_scope, Mapping) else _director_early_journey_scope(None)
    opening_question_id = _text(early_scope["opening_question_ids"][0])
    opening_roles = {
        _text(item).lower() for item in (opening_scope or {}).get("allowed_answer_roles") or () if _text(item)
    }
    specs = {question_id: PURCHASE_JOURNEY_BY_ID[question_id] for question_id in question_ids if question_id in PURCHASE_JOURNEY_BY_ID}
    tag_by_id = {tag.candidate_id: tag for tag in tags if tag.materializable}
    errors: list[str] = []
    selections: list[dict[str, Any]] = []
    selected_ranks: list[StrongClipRank] = []
    selected_ids: set[int] = set()
    seen_questions: set[str] = set()
    quality_rows = _as_mapping_list(data.get("quality_by_question"))
    for raw_row in quality_rows:
        question_id = _text(raw_row.get("purchase_question_id"))
        spec = specs.get(question_id)
        if spec is None or question_id in seen_questions:
            errors.append(f"purchase_quality_question_invalid_or_repeated:{question_id or 'missing'}")
            continue
        seen_questions.add(question_id)
        question = _text(raw_row.get("purchase_question"))
        supports_question_id = _text(raw_row.get("supports_question_id"))
        if question != _text(spec.get("purchase_question")):
            errors.append(f"purchase_quality_question_not_formal:{question_id}")
        if supports_question_id != _text(spec.get("supports_question_id")):
            errors.append(f"purchase_quality_support_not_formal:{question_id}")
        candidates: list[dict[str, Any]] = []
        by_id: dict[int, dict[str, Any]] = {}
        local_ranks: set[int] = set()
        declared_sequence = _as_int_tuple(raw_row.get("selected_candidate_ids"))
        legacy_values = _as_int_tuple(raw_row.get("selected_candidate_id"))
        selected_sequence = _quality_selected_candidate_ids(raw_row)
        for index, raw_candidate in enumerate(_as_mapping_list(raw_row.get("local_candidates")), 1):
            ids = _as_int_tuple(raw_candidate.get("candidate_id"))
            candidate_id = ids[0] if ids else 0
            local_rank = max(1, int(_number(raw_candidate.get("local_rank")) or index))
            answer_role = _text(raw_candidate.get("answer_role")).lower()
            outcome = _text(raw_candidate.get("purchase_outcome"))
            metrics = {
                name: max(1, min(5, int(_number(raw_candidate.get(name)) or 0)))
                for name in ("commercial_impact", "independent_completeness", "specificity", "asr_quality", "semantic_cleanliness", "previous_connection")
            }
            if candidate_id not in tag_by_id or candidate_id in by_id:
                errors.append(f"purchase_quality_candidate_invalid:{question_id}:{candidate_id}")
                continue
            if index > MAX_PURCHASE_QUESTION_QUALITY_CANDIDATES:
                errors.append(f"purchase_quality_candidate_cap_exceeded:{question_id}")
                continue
            if local_rank in local_ranks:
                errors.append(f"purchase_quality_local_rank_repeated:{question_id}:{local_rank}")
                continue
            local_ranks.add(local_rank)
            if answer_role not in set(spec.get("allowed_answer_roles") or ()):
                # Keep an unselected, out-of-question comparison line in the
                # audit response, but never let it become a legal local or
                # final selection.  A selected line remains a hard failure.
                if candidate_id in selected_sequence:
                    errors.append(f"purchase_quality_answer_role_invalid:{question_id}:{candidate_id}:{answer_role or 'missing'}")
                continue
            candidate = {
                "candidate_id": candidate_id, "local_rank": local_rank,
                "purchase_outcome": outcome, "answer_role": answer_role,
                **metrics,
                "spoken_completeness": _text(raw_candidate.get("spoken_completeness")).lower(),
                "spoken_completeness_reason": _text(raw_candidate.get("spoken_completeness_reason")),
                "incremental_purchase_value": _as_bool(raw_candidate.get("incremental_purchase_value")),
                "incremental_purchase_value_reason": _text(raw_candidate.get("incremental_purchase_value_reason")),
                "final_utterance_eligible": _as_bool(raw_candidate.get("final_utterance_eligible")),
                "quality_reason": _text(raw_candidate.get("quality_reason")),
            }
            by_id[candidate_id] = candidate
            candidates.append(candidate)
        if declared_sequence and legacy_values and declared_sequence[0] != legacy_values[0]:
            errors.append(f"purchase_quality_anchor_legacy_mismatch:{question_id}")
        if len(selected_sequence) > MAX_PURCHASE_QUESTION_QUALITY_SELECTED_CANDIDATES:
            errors.append(f"purchase_quality_selected_candidate_cap_exceeded:{question_id}")
        if len(selected_sequence) != len(set(selected_sequence)):
            errors.append(f"purchase_quality_selected_candidate_repeated_in_question:{question_id}")
        selected = [by_id[candidate_id] for candidate_id in selected_sequence if candidate_id in by_id]
        for candidate_id in selected_sequence:
            if candidate_id not in by_id:
                errors.append(f"purchase_quality_selected_candidate_not_ranked:{question_id}:{candidate_id}")
        anchor_id = selected_sequence[0] if selected_sequence else 0
        anchor = selected[0] if selected else None
        if not anchor_id and question_id in required_ids:
            errors.append(f"purchase_quality_core_question_omitted:{question_id}")
        if anchor is not None:
            # The anchor is the one locally top-ranked answer to this buyer
            # question.  Later rows are M2-declared support, not a hidden
            # programmatic expansion of the edit.
            eligible_local_ranks = [
                row["local_rank"] for row in candidates
                if row["final_utterance_eligible"] and row["spoken_completeness"] == "complete" and min(
                    row["asr_quality"], row["semantic_cleanliness"], row["independent_completeness"],
                ) >= 4
            ]
            if eligible_local_ranks and anchor["local_rank"] != min(eligible_local_ranks):
                errors.append(f"purchase_quality_selected_candidate_not_local_top_clean:{question_id}:{anchor_id}")
        selected_role_outcomes: set[tuple[str, str]] = set()
        for selection_index, selected_row in enumerate(selected):
            candidate_id = selected_row["candidate_id"]
            if not selected_row["purchase_outcome"]:
                errors.append(f"purchase_quality_outcome_missing:{question_id}:{candidate_id}")
            reject_reason = _quality_final_utterance_reject_reason(tag_by_id[candidate_id].text)
            if reject_reason:
                errors.append(
                    f"purchase_quality_known_unplayable_utterance:{question_id}:{candidate_id}:{reject_reason}"
                )
            if not selected_row["final_utterance_eligible"] or min(
                selected_row["asr_quality"], selected_row["semantic_cleanliness"], selected_row["independent_completeness"],
            ) < 4:
                errors.append(f"purchase_quality_selected_candidate_not_clean:{question_id}:{candidate_id}")
            if selected_row["spoken_completeness"] != "complete":
                errors.append(f"purchase_quality_spoken_completeness_not_complete:{question_id}:{candidate_id}")
            if selection_index and not selected_row["incremental_purchase_value"]:
                errors.append(f"purchase_quality_support_not_incremental:{question_id}:{candidate_id}")
            answer_role = selected_row["answer_role"]
            role_outcome = (answer_role, selected_row["purchase_outcome"])
            if selection_index and role_outcome in selected_role_outcomes:
                errors.append(
                    f"purchase_quality_support_role_outcome_repeated:{question_id}:{candidate_id}:{answer_role}:{role_outcome[1]}"
                )
            selected_role_outcomes.add(role_outcome)
            if selection_index == 0 and question_id == opening_question_id:
                opening_text = re.sub(r"^[\s，。！？、…]+", "", tag_by_id[candidate_id].text or "")
                if not tag_by_id[candidate_id].hook_eligible:
                    errors.append(f"purchase_quality_opening_not_hook_eligible:{candidate_id}")
                if any(opening_text.startswith(prefix) for prefix in QUALITY_OPENING_LIVE_LEADINS):
                    errors.append(f"purchase_quality_opening_live_leadin:{candidate_id}")
                if float(tag_by_id[candidate_id].duration) < QUALITY_OPENING_MIN_SECONDS:
                    errors.append(f"purchase_quality_opening_too_short:{candidate_id}")
                opening_reject_marker = _quality_opening_reject_marker(tag_by_id[candidate_id].text)
                if opening_reject_marker:
                    errors.append(
                        f"purchase_quality_opening_reject_marker:{candidate_id}:{opening_reject_marker}"
                    )
                if opening_roles and answer_role not in opening_roles:
                    errors.append(f"purchase_quality_opening_role_outside_archetype_scope:{candidate_id}")
                if min(
                    selected_row["commercial_impact"], selected_row["independent_completeness"],
                    selected_row["specificity"], selected_row["asr_quality"], selected_row["semantic_cleanliness"],
                ) < 4:
                    errors.append(f"purchase_quality_opening_not_strong_clean_result:{candidate_id}")
            if candidate_id in selected_ids:
                errors.append(f"purchase_quality_selected_candidate_reused:{candidate_id}")
            selected_ids.add(candidate_id)
            selected_ranks.append(StrongClipRank(
                candidate_id=candidate_id, rank=len(selected_ranks) + 1,
                standalone_strength=float(selected_row["independent_completeness"]),
                hook_power=float(selected_row["commercial_impact"]),
                purchase_value=selected_row["quality_reason"], purchase_outcome=selected_row["purchase_outcome"],
                purchase_question_id=question_id, purchase_question=question,
                supports_question_id=supports_question_id, answer_role=answer_role,
                purchase_question_role=answer_role, answered_question=question,
                evidence_function=answer_role, proof_strength=float(selected_row["specificity"]),
                redundancy_group=f"quality_{question_id}_{answer_role}", fragment=False, visual_dependency=False,
                opening_rank=0, opening_reason="", selection_reason=selected_row["quality_reason"],
            ))
        selections.append({
            "purchase_question_id": question_id, "purchase_question": question,
            "supports_question_id": supports_question_id, "local_candidates": candidates,
            "selected_candidate_ids": list(selected_sequence),
            "selected_candidate_id": anchor_id,
            "support_candidate_ids": list(selected_sequence[1:]),
            "omit_reason": _text(raw_row.get("omit_reason")),
        })
    for question_id in question_ids:
        if question_id not in seen_questions:
            errors.append(f"purchase_quality_question_missing:{question_id}")
    retained = [_text(item) for item in data.get("retained_question_ids") or () if _text(item)]
    retained = list(dict.fromkeys(retained))
    selected_question_ids = [row["purchase_question_id"] for row in selections if row["selected_candidate_id"]]
    if len(retained) != len(selected_question_ids) or set(retained) != set(selected_question_ids):
        errors.append("purchase_quality_retained_question_ids_mismatch")
    dropped = [_text(item) for item in data.get("dropped_optional_question_ids") or () if _text(item)]
    dropped = list(dict.fromkeys(dropped))
    # An Archetype may explicitly discard an optional chapter before it ever
    # enters the current local-quality rows (for example scene Q7 trust, or
    # optional Q2 mechanism).  That is a director deletion receipt, not an
    # attempt by code to invent an omission.
    expected_dropped = {
        question_id for question_id in question_ids if question_id not in selected_question_ids
    }
    allowed_extra_dropped = {
        question_id for question_id in optional_question_ids if question_id not in selected_question_ids
    }
    if (
        not expected_dropped.issubset(set(dropped))
        or not set(dropped).issubset(expected_dropped | allowed_extra_dropped)
        or any(question_id in required_ids for question_id in dropped)
    ):
        errors.append("purchase_quality_dropped_optional_question_ids_mismatch")
    final_plan = data.get("final_plan")
    if not isinstance(final_plan, Mapping):
        # The compact quality response avoids spending the token budget on a
        # second, verbose copy of every local-ranking row.  It still leaves
        # all semantic choices (candidate, order, reason, role) to the model;
        # this code merely materializes that declared structure into M2's
        # existing NarrativePlan schema.
        selected_by_question: dict[str, list[StrongClipRank]] = {}
        for rank in selected_ranks:
            selected_by_question.setdefault(rank.purchase_question_id, []).append(rank)
        order_rows = _as_mapping_list(data.get("final_order"))
        order_question_ids = [_text(row.get("question_id")) for row in order_rows]
        if order_question_ids != retained:
            errors.append("purchase_quality_final_order_question_mismatch")
        compact_plan_ids: list[int] = []
        route_rows: list[dict[str, Any]] = []
        path_rows: list[dict[str, Any]] = []
        chapter_rows: list[dict[str, Any]] = []
        for index, row in enumerate(order_rows, 1):
            question_id = _text(row.get("question_id"))
            ranks = selected_by_question.get(question_id) or []
            candidate_values = _as_int_tuple(row.get("candidate_ids")) or _as_int_tuple(row.get("candidate_id"))
            if not candidate_values or not ranks or tuple(candidate_values) != tuple(rank.candidate_id for rank in ranks):
                errors.append(f"purchase_quality_final_order_candidate_mismatch:{question_id or index}")
                continue
            rank = ranks[0]
            narrative_role = _text(row.get("narrative_role"))
            cognition = _text(row.get("purchase_cognition"))
            why_now = _text(row.get("why_now"))
            why_it_advances = _text(row.get("why_it_advances"))
            domain = _text(row.get("purchase_value_domain"))
            reason = _text(row.get("purchase_value_reason"))
            if not all((narrative_role, cognition, why_now, why_it_advances, domain, reason)):
                errors.append(f"purchase_quality_final_order_detail_missing:{question_id or index}")
            compact_plan_ids.extend(candidate_values)
            route_rows.append({
                "question_id": question_id, "question": rank.purchase_question,
                "journey_role": _text(PURCHASE_JOURNEY_BY_ID[question_id].get("journey_role")),
                "why_now": why_now,
            })
            path_rows.append({
                "step_id": f"P{index}", "chapter_id": f"C{index}",
                "purchase_cognition": cognition, "purchase_question_id": question_id,
                "purchase_question": rank.purchase_question,
                "supports_question_id": rank.supports_question_id,
                "answer_role": rank.answer_role, "answered_question": rank.purchase_question,
                "advance_type": "necessary_stronger_proof" if rank.supports_question_id else "new_purchase_cognition",
                "candidate_ids": list(candidate_values), "why_it_advances": why_it_advances,
            })
            chapter_rows.append({
                "chapter_id": f"C{index}", "narrative_role": narrative_role,
                "candidate_ids": list(candidate_values),
                "purchase_value_dimension": "same_claim_additional_proof" if rank.supports_question_id else "new_outcome",
                "purchase_value_domain": domain,
                "purchase_value_outcomes": [item.purchase_outcome for item in ranks],
                "purchase_value_reason": reason,
                # These are the director's own declared M2 reasons above,
                # projected into the existing NarrativeBeat contract.  They
                # neither choose a candidate nor add a commercial claim, but
                # they let the unchanged M2→M3 consumption audit verify the
                # quality-reordered chapters just like any other M2 plan.
                "goal": cognition,
                "selection_instruction": reason,
                "transition_from_previous": why_now,
                "story_support": why_it_advances,
            })
        final_plan = {
            "purchase_question_route": route_rows,
            "opening_package": dict(data.get("opening_package") or {}),
            "purchase_cognition_path": path_rows,
            "chapters": chapter_rows,
            "story_consumption": dict(data.get("story_consumption") or {}),
            "duration_assessment": {"status": "journey_complete", "reason": "质量优化后的自然结束"},
        }
    final_plan_ids = [
        candidate_id for row in _as_mapping_list(final_plan.get("chapters"))
        for candidate_id in _as_int_tuple(row.get("candidate_ids"))
    ]
    if len(final_plan_ids) != len(selected_ranks) or set(final_plan_ids) != {row.candidate_id for row in selected_ranks}:
        errors.append("purchase_quality_final_plan_candidates_mismatch")
    rank_by_id = {rank.candidate_id: rank for rank in selected_ranks}
    if not errors or "purchase_quality_final_plan_candidates_mismatch" not in errors:
        selected_ranks = [rank_by_id[candidate_id] for candidate_id in final_plan_ids]
    opening = final_plan.get("opening_package") if isinstance(final_plan, Mapping) else {}
    opening_ids = _as_int_tuple(opening.get("hook_candidate_ids")) if isinstance(opening, Mapping) else ()
    # An opening may be a small, M2-selected utterance sequence: the first
    # line remains the actual Hook and must retain the frozen hook permission,
    # while an in-question support can be clean but need not independently be
    # a hook.  The order receipt has already established that every listed ID
    # is the exact anchor-preserving opening micro-sequence; do not collapse it
    # back to one ID here.
    opening_tag = tag_by_id.get(opening_ids[0]) if opening_ids else None
    if (
        not opening_ids
        or any(candidate_id not in selected_ids for candidate_id in opening_ids)
        or opening_tag is None
        or not opening_tag.hook_eligible
    ):
        errors.append("purchase_quality_opening_not_clean_hook_eligible")
    opening_anchor_id = opening_ids[0] if opening_ids else 0
    selected_ranks = [
        replace(rank, rank=index, opening_rank=1, opening_reason="quality_local_top_one") if rank.candidate_id == opening_anchor_id
        else replace(rank, rank=index)
        for index, rank in enumerate(selected_ranks, 1)
    ]
    return {
        "stage": "purchase_journey_local_ranking_then_quality_reorder_v1",
        "current_question_ids": question_ids,
        "quality_by_question": selections,
        "retained_question_ids": retained,
        "dropped_optional_question_ids": dropped,
        "selected_candidate_ids": [rank.candidate_id for rank in selected_ranks],
    }, tuple(selected_ranks), dict(final_plan), list(dict.fromkeys(errors))


def _narrative_enrichment_baseline_payload(
    *, plan_data: Mapping[str, Any], path_audit: Mapping[str, Any],
    ranks: Sequence[StrongClipRank], tags: Sequence[CommerceLiteTag],
) -> list[dict[str, Any]]:
    """Project the already-passed P0.2 cut for one read-only M2 exploration."""
    rank_by_id = {item.candidate_id: item for item in ranks}
    tag_by_id = {item.candidate_id: item for item in tags}
    path_by_chapter = {
        _text(item.get("chapter_id")): item
        for item in _as_mapping_list(plan_data.get("purchase_cognition_path"))
        if _text(item.get("chapter_id"))
    }
    result: list[dict[str, Any]] = []
    for chapter in _as_mapping_list(plan_data.get("chapters")):
        chapter_id = _text(chapter.get("chapter_id"))
        path = path_by_chapter.get(chapter_id, {})
        candidate_ids = _as_int_tuple(chapter.get("candidate_ids"))
        candidate_rows = []
        for candidate_id in candidate_ids:
            rank = rank_by_id.get(candidate_id)
            tag = tag_by_id.get(candidate_id)
            candidate_rows.append({
                "candidate_id": candidate_id,
                "text": tag.text if tag else "",
                "duration_seconds": round(float(tag.duration), 3) if tag else 0.0,
                "purchase_question_id": _text(getattr(rank, "purchase_question_id", "")),
                "purchase_question": _rank_purchase_question(rank) if rank else "",
                "supports_question_id": _text(getattr(rank, "supports_question_id", "")),
                "answer_role": _rank_answer_role(rank) if rank else "",
                "purchase_outcome": _text(getattr(rank, "purchase_outcome", "")),
                "evidence_function": _text(getattr(rank, "evidence_function", "")),
            })
        result.append({
            "chapter_id": chapter_id,
            "narrative_role": _text(chapter.get("narrative_role")),
            "candidate_ids": list(candidate_ids),
            "actual_seconds": round(sum(float(tag_by_id[candidate_id].duration) for candidate_id in candidate_ids if candidate_id in tag_by_id), 3),
            "purchase_question_id": _text(path.get("purchase_question_id")),
            "purchase_question": _text(path.get("purchase_question")),
            "supports_question_id": _text(path.get("supports_question_id")),
            "answer_role": _text(path.get("answer_role")),
            "purchase_cognition": _text(path.get("purchase_cognition")),
            "purchase_value_domain": _text(chapter.get("purchase_value_domain")),
            "purchase_value_outcomes": list(_as_text_tuple(chapter.get("purchase_value_outcomes"))),
            "purchase_value_reason": _text(chapter.get("purchase_value_reason")),
            "spoken_candidates": candidate_rows,
        })
    return result


def _packet_source_context_rows(
    *, source_context_units: Sequence[Mapping[str, Any]] | None,
    safe_candidates: Sequence[PlanningCandidate], executable_evidence: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expose source-local context without turning it into a candidate pool.

    Raw SRT rows can include a unit that is not safe or materializable.  Those
    rows are useful for resolving pronouns and a continuous explanation, but
    are visibly read-only and can never be selected by Packet M2.
    """
    candidates_by_source_id: dict[int, list[int]] = {}
    for candidate in safe_candidates:
        for source_id in candidate.origin_subtitle_ids or (candidate.candidate_id,):
            candidates_by_source_id.setdefault(int(source_id), []).append(candidate.candidate_id)
    raw_units = source_context_units or tuple({
        "id": candidate.origin_subtitle_ids[0] if candidate.origin_subtitle_ids else candidate.candidate_id,
        "start": candidate.start, "end": candidate.end, "text": candidate.text,
    } for candidate in safe_candidates)
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_units, 1):
        if not isinstance(raw, Mapping):
            continue
        source_id = (_as_int_tuple(raw.get("id") or raw.get("subtitle_id") or raw.get("candidate_id")) or (index,))[0]
        start = float(_number(raw.get("start")))
        end = max(start, float(_number(raw.get("end"))))
        linked = tuple(dict.fromkeys(candidates_by_source_id.get(source_id, ())))
        materializable = any(bool(dict(executable_evidence.get(candidate_id) or {}).get("materializable", True)) for candidate_id in linked)
        rows.append({
            "source_unit_id": int(source_id), "start": round(start, 3), "end": round(end, 3),
            "text": _text(raw.get("text")), "linked_candidate_ids": list(linked),
            "safe_executable": bool(linked and materializable), "materializable": bool(materializable),
        })
    return sorted(rows, key=lambda item: (float(item["start"]), float(item["end"]), int(item["source_unit_id"])))


def _packet_window_candidate_payload(
    candidate: PlanningCandidate, *, tag: CommerceLiteTag | None,
    selected_ids: set[int],
) -> dict[str, Any]:
    payload = _quality_candidate_pool_item(tag) if tag is not None else {
        "candidate_id": candidate.candidate_id, "text": candidate.text,
        "materializable": True, "final_utterance_hard_block": "",
    }
    return {
        **payload,
        "source_id": candidate.source_id,
        "start": round(candidate.start, 3), "end": round(candidate.end, 3),
        "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
        "already_in_baseline": candidate.candidate_id in selected_ids,
        "selectable_for_packet": bool(
            candidate.candidate_id not in selected_ids
            and candidate.hook_eligible is not None
            and not _quality_final_utterance_reject_reason(candidate.text)
        ),
    }


def _source_window_payload(
    *, window_id: str, kind: str, start: float, end: float,
    source_id: str, anchor_candidate_id: int, source_rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[PlanningCandidate], tags_by_id: Mapping[int, CommerceLiteTag],
    selected_ids: set[int],
) -> dict[str, Any]:
    context = [
        dict(row) for row in source_rows
        if float(row.get("end") or 0.0) >= start and float(row.get("start") or 0.0) <= end
    ]
    nearby = [
        candidate for candidate in candidates
        if candidate.source_id == source_id and candidate.end >= start and candidate.start <= end
    ]
    payloads = [
        _packet_window_candidate_payload(candidate, tag=tags_by_id.get(candidate.candidate_id), selected_ids=selected_ids)
        for candidate in nearby
    ]
    eligible_ids = [
        int(item["candidate_id"]) for item in payloads
        if bool(item.get("selectable_for_packet")) and bool(item.get("materializable", True))
    ]
    return {
        "source_window_id": window_id,
        "kind": kind,
        "anchor_candidate_id": int(anchor_candidate_id),
        "start": round(max(0.0, start), 3), "end": round(max(start, end), 3),
        "same_source": True, "source_id": source_id,
        "context_units": context,
        "window_candidates": payloads,
        "eligible_packet_candidate_ids": eligible_ids,
    }


def build_chapter_packet_source_windows(
    *, completed_plan_data: Mapping[str, Any], tags: Sequence[CommerceLiteTag],
    safe_candidates: Sequence[PlanningCandidate], executable_evidence: Mapping[int, Mapping[str, Any]],
    source_context_units: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prepare local source windows deterministically; never select a line.

    Existing chapters receive one local window around their current anchor.
    The remaining executable candidates are then grouped only by source time
    density.  Ranking a window by its count is a cost-control operation, not
    a judgement that its words are commercially stronger.
    """
    tags_by_id = {tag.candidate_id: tag for tag in tags}
    candidate_by_id = {candidate.candidate_id: candidate for candidate in safe_candidates}
    selected_ids = {
        candidate_id for chapter in _as_mapping_list(completed_plan_data.get("chapters"))
        for candidate_id in _as_int_tuple(chapter.get("candidate_ids"))
    }
    source_rows = _packet_source_context_rows(
        source_context_units=source_context_units, safe_candidates=safe_candidates,
        executable_evidence=executable_evidence,
    )
    windows: list[dict[str, Any]] = []
    for chapter in _as_mapping_list(completed_plan_data.get("chapters")):
        chapter_id = _text(chapter.get("chapter_id"))
        candidate_ids = _as_int_tuple(chapter.get("candidate_ids"))
        anchor = candidate_by_id.get(candidate_ids[0]) if candidate_ids else None
        if not chapter_id or anchor is None:
            continue
        windows.append(_source_window_payload(
            window_id=f"EW_{chapter_id}_{anchor.candidate_id}", kind="existing_chapter_anchor",
            start=max(0.0, anchor.start - CHAPTER_PACKET_CONTEXT_BEFORE_SECONDS),
            end=anchor.end + CHAPTER_PACKET_CONTEXT_AFTER_SECONDS,
            source_id=anchor.source_id, anchor_candidate_id=anchor.candidate_id,
            source_rows=source_rows, candidates=safe_candidates, tags_by_id=tags_by_id, selected_ids=selected_ids,
        ))

    remaining = sorted((
        candidate for candidate in safe_candidates
        if candidate.candidate_id not in selected_ids
        and candidate.candidate_id in tags_by_id
        and tags_by_id[candidate.candidate_id].materializable
        and not _quality_final_utterance_reject_reason(candidate.text)
    ), key=lambda item: (item.source_id, item.start, item.end, item.candidate_id))
    clusters: list[list[PlanningCandidate]] = []
    current: list[PlanningCandidate] = []
    current_source = ""
    current_start = 0.0
    current_end = 0.0
    for candidate in remaining:
        extends_current = bool(
            current and candidate.source_id == current_source
            and candidate.start <= current_end + CHAPTER_PACKET_DISCOVERY_GAP_SECONDS
            and max(current_end, candidate.end) - current_start <= CHAPTER_PACKET_DISCOVERY_MAX_SECONDS
        )
        if not extends_current:
            if current:
                clusters.append(current)
            current = [candidate]
            current_source, current_start, current_end = candidate.source_id, candidate.start, candidate.end
        else:
            current.append(candidate)
            current_end = max(current_end, candidate.end)
    if current:
        clusters.append(current)
    dense = [cluster for cluster in clusters if len(cluster) >= 2]
    ranked_clusters = sorted(
        dense,
        key=lambda cluster: (-len(cluster), -(cluster[-1].end - cluster[0].start), cluster[0].start, cluster[0].candidate_id),
    )
    exposed_clusters = ranked_clusters[:MAX_CHAPTER_PACKET_DISCOVERY_WINDOWS]
    for index, cluster in enumerate(exposed_clusters, 1):
        windows.append(_source_window_payload(
            window_id=f"DW_{index:02d}_{cluster[0].candidate_id}", kind="new_structure_discovery",
            start=max(0.0, cluster[0].start - CHAPTER_PACKET_CONTEXT_BEFORE_SECONDS),
            end=cluster[-1].end + CHAPTER_PACKET_CONTEXT_AFTER_SECONDS,
            source_id=cluster[0].source_id, anchor_candidate_id=0,
            source_rows=source_rows, candidates=safe_candidates, tags_by_id=tags_by_id, selected_ids=selected_ids,
        ))
    audit = {
        "context_before_seconds": CHAPTER_PACKET_CONTEXT_BEFORE_SECONDS,
        "context_after_seconds": CHAPTER_PACKET_CONTEXT_AFTER_SECONDS,
        "discovery_gap_seconds": CHAPTER_PACKET_DISCOVERY_GAP_SECONDS,
        "discovery_max_seconds": CHAPTER_PACKET_DISCOVERY_MAX_SECONDS,
        "existing_anchor_window_count": sum(1 for item in windows if item["kind"] == "existing_chapter_anchor"),
        "remaining_safe_executable_candidate_pool_size": len(remaining),
        "discovery_dense_window_count": len(dense),
        "discovery_windows_exposed_count": len(exposed_clusters),
        "discovery_windows_not_exposed_for_cost_control": max(0, len(dense) - len(exposed_clusters)),
        "source_windows": [
            {key: item[key] for key in ("source_window_id", "kind", "anchor_candidate_id", "start", "end", "source_id", "eligible_packet_candidate_ids")}
            for item in windows
        ],
    }
    return windows, audit


def _chapter_packet_baseline_payload(
    *, completed_plan_data: Mapping[str, Any], completed_path_audit: Mapping[str, Any],
    completed_ranks: Sequence[StrongClipRank], tags: Sequence[CommerceLiteTag],
) -> list[dict[str, Any]]:
    """The finished P0.2 chapters, kept read-only for a packet decision."""
    return _narrative_enrichment_baseline_payload(
        plan_data=completed_plan_data, path_audit=completed_path_audit,
        ranks=completed_ranks, tags=tags,
    )


def build_chapter_packet_prompt(
    *, strategy: Any, director_strategy_contract: Mapping[str, Any],
    narrative_depth: Mapping[str, Any], completed_plan_data: Mapping[str, Any],
    completed_path_audit: Mapping[str, Any], completed_ranks: Sequence[StrongClipRank],
    tags: Sequence[CommerceLiteTag], source_windows: Sequence[Mapping[str, Any]],
    target_duration: float,
) -> str:
    """One M2 packet decision over local source context, not per-line scoring."""
    baseline = _chapter_packet_baseline_payload(
        completed_plan_data=completed_plan_data, completed_path_audit=completed_path_audit,
        completed_ranks=completed_ranks, tags=tags,
    )
    return "\n".join((
        "你仍在同一个 M2 商业导演内。当前购买旅程和 P0.2 Final Utterance Quality 已通过，但成片可能因 Candidate 化而只剩卖点摘要。现在只允许做一次 P0.4 Chapter Packet Builder：从真实直播局部 Source Window 恢复值得保留的完整微叙事章节。",
        "这不是为了达到目标时长，不是 Replan/Completion/Compression，也不是逐条候选打分。若没有完整、干净且能明显改善成片的 Packet，必须返回 no_worthwhile_packet，保留原片。",
        "程序给出的 Source Window 只是上下文：其中 safe_executable=false 或 selectable_for_packet=false 的行永远不可选择。你只能选择窗口里 selectable_for_packet=true 的真实 candidate_id；程序不替你选句、补句、重排或改写字幕。",
        "Existing Chapter Packet：可围绕当前章节 Anchor 增加真实 support。support 必须从结果/原因/证明/体验/使用状态中带来新的 sub-value，不能换句话重复；同源局部连续优先。基础章节和 Opening 不可删除、替换或重排，新增 support 只能附在原章节之后。",
        "New Structure Packet：可从 discovery window 创建 E1/E2…，但必须是当前视频没有表达过的完整小故事；必须有新 purchase_value、明确 chapter_intent、同源局部进展和与当前 core desire 的关系。",
        "跨窗口只在局部窗口无法形成完整 Packet 时允许，且必须写 cross_window_reason；不得跨几十分钟拼凑相似卖点。每个最终新增口播必须：commercial_impact、independent_completeness、specificity、asr_quality、semantic_cleanliness、previous_connection 全部至少4；spoken_completeness=complete；final_utterance_eligible=true；incremental_purchase_value=true。不得复活 438、217 或已知 ASR/残句。",
        "每个 Packet 的 progression 必须明确每句相比前一句新增什么；packet_internal_flow 和 packet_information_progression 必须通过。可以在同一回复的 final_packet_quality 中主动删除你刚发现但会拖沓或打断节奏的新增 Packet/support。",
        "M1 只读来源：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前 Director Narrative Contract（本片权威意图）：",
        json.dumps(dict(director_strategy_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "当前 Narrative Depth / Blueprint（已完成，不要求补完所有 Q）：",
        json.dumps(dict(narrative_depth or {}), ensure_ascii=False, separators=(",", ":")),
        "冻结的 P0.2 已完成章节：",
        json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
        "Source Window Candidates（原始上下文仅供理解；只可选 selectable_for_packet=true）：",
        json.dumps(list(source_windows), ensure_ascii=False, separators=(",", ":")),
        f"目标时长 {max(1.0, float(target_duration)):.1f}s，仅作软参考。",
        "只返回 JSON：",
        json.dumps({
            "existing_chapter_packets": [{
                "packet_id": "P_C5_01", "packet_type": "existing_chapter_expansion", "chapter_id": "C5",
                "chapter_intent": "", "purchase_value": "", "anchor_candidate_id": 0,
                "source_window_id": "", "ordered_candidate_ids": [0], "support_candidate_ids": [0],
                "progression": [{"candidate_id": 0, "function": "result/mechanism/proof/experience/use_case", "new_sub_value": "", "candidate_annotation": {}}],
                "why_this_is_a_packet": "", "core_desire_compatibility": "", "net_improvement": "", "cross_window_reason": "",
            }],
            "new_structure_packets": [{
                "packet_id": "P_E1_01", "packet_type": "new_structure_chapter", "enrichment_chapter_id": "E1",
                "chapter_intent": "", "purchase_value": "", "purchase_value_domain": "", "purchase_value_outcomes": [""],
                "narrative_role": "scene/support/proof", "anchor_candidate_id": 0, "source_window_id": "",
                "ordered_candidate_ids": [0], "candidate_ids": [0],
                "progression": [{"candidate_id": 0, "function": "result/mechanism/proof/experience/use_case", "new_sub_value": "", "candidate_annotation": {}}],
                "recommended_insert_after": "C5", "core_desire_compatibility": "", "why_this_is_a_packet": "", "why_add": "", "net_improvement": "", "cross_window_reason": "",
            }],
            "rejected_source_windows": [{"source_window_id": "", "candidate_ids": [0], "reason": "duplicate/weak/off_theme/poor_flow/not_a_packet"}],
            "explored_source_window_ids": [""],
            "final_order": [{"chapter_ref": "C1", "candidate_ids": [0], "why_now": ""}],
            "final_packet_quality": {
                "whole_transcript_passed": True, "opening_unchanged": True, "natural_flow": True,
                "no_repeat": True, "no_drag": True, "ending_natural": True,
                "packet_internal_flow": True, "packet_information_progression": True, "packet_redundancy": True,
                "packet_to_previous_chapter_transition": True, "packet_to_next_chapter_transition": True,
                "whole_video_pacing": True, "reason": "", "dropped_new_candidate_ids": [], "dropped_new_packet_ids": [],
            },
            "single_candidate_enrichment_recommended": False,
            "packet_status": "packet_enriched/no_worthwhile_packet",
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def build_narrative_enrichment_prompt(
    *, strategy: Any, director_strategy_contract: Mapping[str, Any],
    narrative_depth: Mapping[str, Any], completed_plan_data: Mapping[str, Any],
    completed_path_audit: Mapping[str, Any], completed_ranks: Sequence[StrongClipRank],
    tags: Sequence[CommerceLiteTag], remaining_tags: Sequence[CommerceLiteTag],
    target_duration: float,
) -> str:
    """Ask the existing M2 director once whether a clean finished cut merits depth."""
    baseline = _narrative_enrichment_baseline_payload(
        plan_data=completed_plan_data, path_audit=completed_path_audit,
        ranks=completed_ranks, tags=tags,
    )
    baseline_ids = [
        candidate_id for chapter in baseline
        for candidate_id in _as_int_tuple(chapter.get("candidate_ids"))
    ]
    return "\n".join((
        "你仍在同一个 M2 商业导演内，现有购买旅程和 P0.2 Final Utterance Quality 已经通过。现在只允许进行一次 P0.3 Narrative Enrichment 探索：检查完整剩余 safe executable 候选池，判断是否存在能让成片更丰富、但不注水的新购买认知。",
        "这不是 Replan、Completion、Compression，也不是为了达到目标秒数。目标 45–75 秒只是允许探索深度的软范围；当前片已经可以自然结束。若没有真正更强的内容，必须返回 no_worthwhile_enrichment 并完整保留当前片。",
        "基础章节、基础 candidate_id、Opening、现有购买路径全部冻结：不能删除、替换、重排或改写它们。你只能：(A) 为某个现有章节在锚点后增加 0–2 条干净且真正新增认知的 support；(B) 插入一个兼容的新商业章节 E1/E2… 。程序不选句，只会校验你声明的候选、质量、关系、重复、来源和真实时长。",
        "现有章节加 support 的条件：必须服务同一购买问题，带来新的 sub-outcome / 新证据功能；若 answer_role 相同，必须明确 clear_novel_proof=true 且写出 novel_proof_reason。默认一个章节最多 3 条总口播（既有锚点加 support）；第 4 条只在 support_count_explanation 具体说明为什么删任一句会损失不同购买认知时允许。",
        "新章节不受 Q1–Q7 枚举限制，但必须有动态 enrichment_chapter_id（E1…）、chapter_intent、new_purchase_value、明确的兼容性与插入位置。它可以来自 primary/supporting/bridge/unhero 的真实安全候选，但不得改写 M1、切换 Archetype 或把已讲过的肩部/显瘦/尺码/舒适事实换一种说法再讲一次。",
        "所有新增原话必须重新按 P0.2 最终口播标准审：commercial_impact、independent_completeness、specificity、asr_quality、semantic_cleanliness、previous_connection 都为 1–5；所有项至少 4，spoken_completeness=complete，final_utterance_eligible=true，incremental_purchase_value=true。残句、直播接话、ASR 怪句、截断、无来源指代一律不能加入。明确禁止复活 438/217 或任何包含“35厘米”“肩干嘛”“A类母婴店”“像100斤葡萄”等异常的口播。",
        "完成探索后，在同一回复内做一次全片 P0.2 Quality 复核：以真实【上一句→当前句→下一句】检查是否自然、是否重复、是否拖沓；Opening 必须原样保留，结尾必须自然。你可以在这一次复核中主动删除刚才发现但不值得加入的新 support / 新章节，或调整新 E 章节的位置；不能删旧章节。",
        "停止规则：只有同题重复、与 archetype/core desire 不兼容、口播不干净、或全片更拖时，拒绝该机会；没有新的高价值章节即自然结束。",
        "M1 只读来源：",
        json.dumps(_compact_story(strategy), ensure_ascii=False, separators=(",", ":")),
        "当前 Director Narrative Contract（本片权威意图）：",
        json.dumps(dict(director_strategy_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "当前 Narrative Depth / Blueprint（已完成，不要求再补 Q）：",
        json.dumps(dict(narrative_depth or {}), ensure_ascii=False, separators=(",", ":")),
        "已通过 P0.2 的当前完整章节（冻结，含真实原话和已有购买关系）：",
        json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
        "当前已使用 candidate_id（绝不可复用）：",
        json.dumps(baseline_ids, ensure_ascii=False, separators=(",", ":")),
        "完整剩余 executable-safe candidate pool（不是 Top 12；只能从这里声明新增候选）：",
        json.dumps([_quality_candidate_pool_item(tag) for tag in remaining_tags], ensure_ascii=False, separators=(",", ":")),
        f"目标时长 {max(1.0, float(target_duration)):.1f}s，仅为软目标。",
        "只返回 JSON：",
        json.dumps({
            "existing_chapter_enrichment": [{
                "chapter_id": "C4", "current_value": "", "candidate_ids": [0],
                "candidate_annotations": [{
                    "candidate_id": 0, "answer_role": "result/mechanism/proof/risk_remove/comfort/scene/styling/trust", "purchase_outcome": "",
                    "commercial_impact": 1, "independent_completeness": 1, "specificity": 1, "asr_quality": 1,
                    "semantic_cleanliness": 1, "previous_connection": 1, "spoken_completeness": "complete/dependent/incomplete",
                    "final_utterance_eligible": False, "incremental_purchase_value": False,
                    "incremental_purchase_value_reason": "", "clear_novel_proof": False, "novel_proof_reason": "",
                    "quality_reason": "",
                }],
                "new_sub_value": "", "why_add": "", "net_improvement": "", "support_count_explanation": "",
            }],
            "new_structure_chapters": [{
                "enrichment_chapter_id": "E1", "chapter_intent": "", "new_purchase_value": "", "candidate_ids": [0],
                "candidate_annotations": [{
                    "candidate_id": 0, "answer_role": "result/mechanism/proof/risk_remove/comfort/scene/styling/trust", "purchase_outcome": "",
                    "commercial_impact": 1, "independent_completeness": 1, "specificity": 1, "asr_quality": 1,
                    "semantic_cleanliness": 1, "previous_connection": 1, "spoken_completeness": "complete/dependent/incomplete",
                    "final_utterance_eligible": False, "incremental_purchase_value": False,
                    "incremental_purchase_value_reason": "", "clear_novel_proof": False, "novel_proof_reason": "",
                    "quality_reason": "",
                }],
                "purchase_value_domain": "", "purchase_value_outcomes": [""], "narrative_role": "new_value/scene/comfort/risk_reduction",
                "recommended_insert_after": "C4", "compatibility_with_core_desire": "", "why_add": "", "net_improvement": "", "support_count_explanation": "",
            }],
            "rejected_opportunities": [{"candidate_ids": [0], "reason": "重复/不干净/不兼容/不值得增加"}],
            "final_order": [{"chapter_ref": "C1", "candidate_ids": [0], "why_now": ""}],
            "enrichment_quality": {
                "whole_transcript_passed": True, "opening_unchanged": True, "natural_flow": True,
                "no_repeat": True, "no_drag": True, "ending_natural": True, "reason": "",
                "dropped_new_candidate_ids": [], "dropped_new_chapter_ids": [],
            },
            "enrichment_status": "enriched/no_worthwhile_enrichment",
        }, ensure_ascii=False, separators=(",", ":")),
    ))


def _narrative_enrichment_annotation_rank(
    *, raw: Mapping[str, Any], tag: CommerceLiteTag | None, question_id: str,
    question: str, supports_question_id: str, allowed_roles: set[str],
    rank: int, errors: list[str], scope: str,
) -> StrongClipRank | None:
    """Validate an AI-declared extra line without giving it a semantic score."""
    candidate_ids = _as_int_tuple(raw.get("candidate_id"))
    candidate_id = candidate_ids[0] if candidate_ids else 0
    if tag is None or candidate_id != tag.candidate_id:
        errors.append(f"narrative_enrichment_candidate_not_in_remaining_pool:{scope}:{candidate_id}")
        return None
    role = _text(raw.get("answer_role")).lower()
    outcome = _text(raw.get("purchase_outcome"))
    if role not in allowed_roles:
        errors.append(f"narrative_enrichment_answer_role_invalid:{scope}:{candidate_id}:{role or 'missing'}")
    if not outcome:
        errors.append(f"narrative_enrichment_purchase_outcome_missing:{scope}:{candidate_id}")
    metrics = {
        name: max(1, min(5, int(_number(raw.get(name)) or 0)))
        for name in (
            "commercial_impact", "independent_completeness", "specificity", "asr_quality",
            "semantic_cleanliness", "previous_connection",
        )
    }
    if min(metrics.values()) < 4:
        errors.append(f"narrative_enrichment_quality_below_p02_floor:{scope}:{candidate_id}")
    if _text(raw.get("spoken_completeness")).lower() != "complete":
        errors.append(f"narrative_enrichment_spoken_completeness_not_complete:{scope}:{candidate_id}")
    if not _as_bool(raw.get("final_utterance_eligible")):
        errors.append(f"narrative_enrichment_final_utterance_not_eligible:{scope}:{candidate_id}")
    if not _as_bool(raw.get("incremental_purchase_value")) or not _text(raw.get("incremental_purchase_value_reason")):
        errors.append(f"narrative_enrichment_not_incremental:{scope}:{candidate_id}")
    reject_reason = _quality_final_utterance_reject_reason(tag.text)
    if reject_reason:
        errors.append(f"narrative_enrichment_known_unplayable_utterance:{scope}:{candidate_id}:{reject_reason}")
    return StrongClipRank(
        candidate_id=candidate_id, rank=rank,
        standalone_strength=float(metrics["independent_completeness"]),
        hook_power=float(metrics["commercial_impact"]),
        purchase_value=_text(raw.get("quality_reason")) or _text(raw.get("incremental_purchase_value_reason")),
        purchase_outcome=outcome, purchase_question_id=question_id, purchase_question=question,
        supports_question_id=supports_question_id, answer_role=role,
        purchase_question_role=role, answered_question=question, evidence_function=role,
        proof_strength=float(metrics["specificity"]),
        redundancy_group=f"enrichment_{question_id}_{role}_{outcome}",
        fragment=False, visual_dependency=False, opening_rank=0, opening_reason="",
        selection_reason=_text(raw.get("quality_reason")) or _text(raw.get("incremental_purchase_value_reason")),
    )


def _parse_narrative_enrichment(
    data: Mapping[str, Any], *, baseline_plan_data: Mapping[str, Any],
    baseline_path_audit: Mapping[str, Any], baseline_ranks: Sequence[StrongClipRank],
    remaining_tags: Sequence[CommerceLiteTag],
    existing_candidate_limit: int | None = MAX_NARRATIVE_ENRICHMENT_CANDIDATES_PER_CHAPTER,
    existing_exception_limit: int | None = MAX_NARRATIVE_ENRICHMENT_EXCEPTION_CANDIDATES_PER_CHAPTER,
    new_candidate_limit: int | None = MAX_NARRATIVE_ENRICHMENT_CANDIDATES_PER_CHAPTER,
    new_exception_limit: int | None = MAX_NARRATIVE_ENRICHMENT_EXCEPTION_CANDIDATES_PER_CHAPTER,
) -> tuple[dict[str, Any], tuple[StrongClipRank, ...], dict[str, Any], list[str]]:
    """Materialize only the single-pass M2 enrichment declaration.

    The parser deliberately cannot infer an addition.  It can only project the
    candidate IDs and commercial relations M2 already declared, then reject a
    declaration that damages the P0.2 baseline or duplicates it.
    """
    errors: list[str] = []
    tag_by_id = {tag.candidate_id: tag for tag in remaining_tags}
    base_chapters = _as_mapping_list(baseline_plan_data.get("chapters"))
    base_paths = _as_mapping_list(baseline_plan_data.get("purchase_cognition_path"))
    base_routes = _as_mapping_list(baseline_plan_data.get("purchase_question_route"))
    base_chapter_by_id = {_text(row.get("chapter_id")): row for row in base_chapters if _text(row.get("chapter_id"))}
    base_path_by_chapter = {_text(row.get("chapter_id")): row for row in base_paths if _text(row.get("chapter_id"))}
    base_route_by_question = {_text(row.get("question_id")): row for row in base_routes if _text(row.get("question_id"))}
    base_chapter_ids = list(base_chapter_by_id)
    base_rank_by_id = {item.candidate_id: item for item in baseline_ranks}
    base_candidate_ids = {
        candidate_id for row in base_chapters
        for candidate_id in _as_int_tuple(row.get("candidate_ids"))
    }
    base_outcomes = {
        _text(item.purchase_outcome) for item in baseline_ranks if _text(item.purchase_outcome)
    }
    base_pairs_by_question: dict[str, set[tuple[str, str]]] = {}
    for item in baseline_ranks:
        base_pairs_by_question.setdefault(_text(item.purchase_question_id), set()).add((
            _rank_answer_role(item), _text(item.purchase_outcome),
        ))

    status = _text(data.get("enrichment_status")).lower()
    if status not in {"enriched", "no_worthwhile_enrichment"}:
        errors.append("narrative_enrichment_status_invalid")
    seen_new_candidate_ids: set[int] = set()
    existing_rows: dict[str, dict[str, Any]] = {}
    existing_extra_ranks: dict[str, list[StrongClipRank]] = {}
    for raw in _as_mapping_list(data.get("existing_chapter_enrichment")):
        chapter_id = _text(raw.get("chapter_id"))
        if chapter_id not in base_chapter_by_id or chapter_id in existing_rows:
            errors.append(f"narrative_enrichment_existing_chapter_invalid_or_repeated:{chapter_id or 'missing'}")
            continue
        candidate_ids = _as_int_tuple(raw.get("candidate_ids"))
        annotations = _as_mapping_list(raw.get("candidate_annotations"))
        if not candidate_ids or tuple(candidate_ids) != tuple(_as_int_tuple(item.get("candidate_id"))[:1][0] if _as_int_tuple(item.get("candidate_id")) else 0 for item in annotations):
            errors.append(f"narrative_enrichment_existing_annotation_candidate_mismatch:{chapter_id}")
        if not all(_text(raw.get(key)) for key in ("current_value", "new_sub_value", "why_add", "net_improvement")):
            errors.append(f"narrative_enrichment_existing_value_detail_missing:{chapter_id}")
        path = base_path_by_chapter.get(chapter_id, {})
        question_id = _text(path.get("purchase_question_id"))
        question = _text(path.get("purchase_question"))
        supports_question_id = _text(path.get("supports_question_id"))
        allowed_roles = set((PURCHASE_JOURNEY_BY_ID.get(question_id) or {}).get("allowed_answer_roles") or ())
        if not question_id or not allowed_roles:
            errors.append(f"narrative_enrichment_existing_chapter_relation_missing:{chapter_id}")
        extra_ranks: list[StrongClipRank] = []
        existing_pairs = set(base_pairs_by_question.get(question_id, set()))
        for offset, annotation in enumerate(annotations, 1):
            candidate_id = (_as_int_tuple(annotation.get("candidate_id")) or (0,))[0]
            if candidate_id in seen_new_candidate_ids or candidate_id in base_candidate_ids:
                errors.append(f"narrative_enrichment_candidate_reused:{chapter_id}:{candidate_id}")
            seen_new_candidate_ids.add(candidate_id)
            rank = _narrative_enrichment_annotation_rank(
                raw=annotation, tag=tag_by_id.get(candidate_id), question_id=question_id,
                question=question, supports_question_id=supports_question_id,
                allowed_roles=allowed_roles, rank=len(baseline_ranks) + len(extra_ranks) + 1,
                errors=errors, scope=chapter_id,
            )
            if rank is not None:
                pair = (_rank_answer_role(rank), _text(rank.purchase_outcome))
                if pair in existing_pairs:
                    errors.append(f"narrative_enrichment_existing_role_outcome_repeated:{chapter_id}:{candidate_id}")
                if any(previous_role == pair[0] for previous_role, _ in existing_pairs) and not (
                    _as_bool(annotation.get("clear_novel_proof")) and _text(annotation.get("novel_proof_reason"))
                ):
                    errors.append(f"narrative_enrichment_existing_same_role_without_novel_proof:{chapter_id}:{candidate_id}")
                existing_pairs.add(pair)
                extra_ranks.append(rank)
        total_candidates = len(_as_int_tuple(base_chapter_by_id[chapter_id].get("candidate_ids"))) + len(candidate_ids)
        if existing_exception_limit is not None and total_candidates > existing_exception_limit:
            errors.append(f"narrative_enrichment_existing_candidate_cap_exceeded:{chapter_id}")
        elif (
            existing_candidate_limit is not None and total_candidates > existing_candidate_limit
            and not _text(raw.get("support_count_explanation"))
        ):
            errors.append(f"narrative_enrichment_existing_exception_explanation_missing:{chapter_id}")
        existing_rows[chapter_id] = dict(raw)
        existing_extra_ranks[chapter_id] = extra_ranks

    new_rows: dict[str, dict[str, Any]] = {}
    new_ranks: dict[str, list[StrongClipRank]] = {}
    for raw in _as_mapping_list(data.get("new_structure_chapters")):
        chapter_id = _text(raw.get("enrichment_chapter_id"))
        if not re.fullmatch(r"E[1-9]\d*", chapter_id) or chapter_id in new_rows or chapter_id in base_chapter_by_id:
            errors.append(f"narrative_enrichment_new_chapter_id_invalid_or_repeated:{chapter_id or 'missing'}")
            continue
        candidate_ids = _as_int_tuple(raw.get("candidate_ids"))
        annotations = _as_mapping_list(raw.get("candidate_annotations"))
        if not candidate_ids or tuple(candidate_ids) != tuple(_as_int_tuple(item.get("candidate_id"))[:1][0] if _as_int_tuple(item.get("candidate_id")) else 0 for item in annotations):
            errors.append(f"narrative_enrichment_new_annotation_candidate_mismatch:{chapter_id}")
        required_details = (
            "chapter_intent", "new_purchase_value", "purchase_value_domain", "narrative_role",
            "recommended_insert_after", "compatibility_with_core_desire", "why_add", "net_improvement",
        )
        if not all(_text(raw.get(key)) for key in required_details):
            errors.append(f"narrative_enrichment_new_value_detail_missing:{chapter_id}")
        if _text(raw.get("recommended_insert_after")) not in base_chapter_by_id:
            errors.append(f"narrative_enrichment_new_insert_after_invalid:{chapter_id}")
        outcomes = _as_text_tuple(raw.get("purchase_value_outcomes"))
        if not outcomes:
            errors.append(f"narrative_enrichment_new_outcomes_missing:{chapter_id}")
        if set(outcomes).intersection(base_outcomes):
            errors.append(f"narrative_enrichment_new_purchase_outcome_repeated:{chapter_id}")
        ranks_for_chapter: list[StrongClipRank] = []
        for offset, annotation in enumerate(annotations, 1):
            candidate_id = (_as_int_tuple(annotation.get("candidate_id")) or (0,))[0]
            if candidate_id in seen_new_candidate_ids or candidate_id in base_candidate_ids:
                errors.append(f"narrative_enrichment_candidate_reused:{chapter_id}:{candidate_id}")
            seen_new_candidate_ids.add(candidate_id)
            rank = _narrative_enrichment_annotation_rank(
                raw=annotation, tag=tag_by_id.get(candidate_id), question_id=chapter_id,
                question=_text(raw.get("chapter_intent")), supports_question_id="",
                allowed_roles=set(NARRATIVE_ENRICHMENT_ALLOWED_ANSWER_ROLES),
                rank=len(baseline_ranks) + len(ranks_for_chapter) + 1,
                errors=errors, scope=chapter_id,
            )
            if rank is not None:
                ranks_for_chapter.append(rank)
        if {item.purchase_outcome for item in ranks_for_chapter} != set(outcomes):
            errors.append(f"narrative_enrichment_new_outcome_annotation_mismatch:{chapter_id}")
        if new_exception_limit is not None and len(candidate_ids) > new_exception_limit:
            errors.append(f"narrative_enrichment_new_candidate_cap_exceeded:{chapter_id}")
        elif (
            new_candidate_limit is not None and len(candidate_ids) > new_candidate_limit
            and not _text(raw.get("support_count_explanation"))
        ):
            errors.append(f"narrative_enrichment_new_exception_explanation_missing:{chapter_id}")
        new_rows[chapter_id] = dict(raw)
        new_ranks[chapter_id] = ranks_for_chapter

    has_additions = bool(existing_rows or new_rows)
    if status == "enriched" and not has_additions:
        errors.append("narrative_enrichment_enriched_without_addition")
    if status == "no_worthwhile_enrichment" and has_additions:
        errors.append("narrative_enrichment_no_worthwhile_with_addition")

    final_order = _as_mapping_list(data.get("final_order"))
    order_refs = [_text(row.get("chapter_ref")) for row in final_order]
    all_refs = set(base_chapter_ids).union(new_rows)
    if len(order_refs) != len(set(order_refs)) or set(order_refs) != all_refs:
        errors.append("narrative_enrichment_final_order_chapter_refs_mismatch")
    if [item for item in order_refs if item in base_chapter_by_id] != base_chapter_ids:
        errors.append("narrative_enrichment_baseline_order_changed")
    expected_by_ref: dict[str, tuple[int, ...]] = {
        chapter_id: _as_int_tuple(base_chapter_by_id[chapter_id].get("candidate_ids"))
        + _as_int_tuple(existing_rows.get(chapter_id, {}).get("candidate_ids"))
        for chapter_id in base_chapter_ids
    }
    expected_by_ref.update({
        chapter_id: _as_int_tuple(row.get("candidate_ids"))
        for chapter_id, row in new_rows.items()
    })
    for order_row in final_order:
        chapter_ref = _text(order_row.get("chapter_ref"))
        if tuple(_as_int_tuple(order_row.get("candidate_ids"))) != expected_by_ref.get(chapter_ref, ()):
            errors.append(f"narrative_enrichment_final_order_candidate_mismatch:{chapter_ref or 'missing'}")
        if not _text(order_row.get("why_now")):
            errors.append(f"narrative_enrichment_final_order_why_now_missing:{chapter_ref or 'missing'}")
    order_index = {chapter_id: index for index, chapter_id in enumerate(order_refs)}
    for chapter_id, raw in new_rows.items():
        insert_after = _text(raw.get("recommended_insert_after"))
        if order_index.get(chapter_id, -1) <= order_index.get(insert_after, -1):
            errors.append(f"narrative_enrichment_new_chapter_position_invalid:{chapter_id}")

    quality = dict(data.get("enrichment_quality") or {})
    for key in (
        "whole_transcript_passed", "opening_unchanged", "natural_flow", "no_repeat", "no_drag", "ending_natural",
    ):
        if not _as_bool(quality.get(key)):
            errors.append(f"narrative_enrichment_p02_quality_not_passed:{key}")
    if not _text(quality.get("reason")):
        errors.append("narrative_enrichment_quality_reason_missing")
    selected_extra_ids = set(seen_new_candidate_ids)
    if selected_extra_ids.intersection(_as_int_tuple(quality.get("dropped_new_candidate_ids"))):
        errors.append("narrative_enrichment_quality_dropped_candidate_still_selected")
    if set(new_rows).intersection(_as_text_tuple(quality.get("dropped_new_chapter_ids"))):
        errors.append("narrative_enrichment_quality_dropped_chapter_still_selected")

    ordered_routes: list[dict[str, Any]] = []
    ordered_paths: list[dict[str, Any]] = []
    ordered_chapters: list[dict[str, Any]] = []
    for index, order_row in enumerate(final_order, 1):
        chapter_id = _text(order_row.get("chapter_ref"))
        candidate_ids = list(_as_int_tuple(order_row.get("candidate_ids")))
        if chapter_id in base_chapter_by_id:
            source_path = dict(base_path_by_chapter.get(chapter_id) or {})
            source_chapter = dict(base_chapter_by_id[chapter_id])
            question_id = _text(source_path.get("purchase_question_id"))
            source_route = dict(base_route_by_question.get(question_id) or {})
            source_route["why_now"] = _text(order_row.get("why_now"))
            ordered_routes.append(source_route)
            source_path.update({"step_id": f"P{index}", "chapter_id": chapter_id, "candidate_ids": candidate_ids})
            ordered_paths.append(source_path)
            source_chapter["candidate_ids"] = candidate_ids
            extra_outcomes = [item.purchase_outcome for item in existing_extra_ranks.get(chapter_id, ())]
            source_chapter["purchase_value_outcomes"] = list(dict.fromkeys(
                list(_as_text_tuple(source_chapter.get("purchase_value_outcomes"))) + extra_outcomes
            ))
            ordered_chapters.append(source_chapter)
            continue
        raw = new_rows.get(chapter_id, {})
        ranks_for_chapter = new_ranks.get(chapter_id, ())
        chapter_intent = _text(raw.get("chapter_intent"))
        ordered_routes.append({
            "question_id": chapter_id, "question": chapter_intent,
            "journey_role": "narrative_enrichment", "why_now": _text(order_row.get("why_now")),
        })
        ordered_paths.append({
            "step_id": f"P{index}", "chapter_id": chapter_id,
            "purchase_cognition": _text(raw.get("new_purchase_value")),
            "purchase_question_id": chapter_id, "purchase_question": chapter_intent,
            "supports_question_id": "", "answer_role": _rank_answer_role(ranks_for_chapter[0]) if ranks_for_chapter else "",
            "answered_question": chapter_intent, "advance_type": "new_purchase_cognition",
            "candidate_ids": candidate_ids, "why_it_advances": _text(raw.get("net_improvement")),
        })
        ordered_chapters.append({
            "chapter_id": chapter_id, "narrative_role": _text(raw.get("narrative_role")),
            "goal": chapter_intent, "candidate_ids": candidate_ids,
            "asset_tier": "", "story_support": _text(raw.get("compatibility_with_core_desire")),
            "commerce_beat_id": chapter_id, "value_dimension": "",
            "purchase_value_dimension": "new_outcome",
            "purchase_value_domain": _text(raw.get("purchase_value_domain")),
            "purchase_value_outcomes": list(_as_text_tuple(raw.get("purchase_value_outcomes"))),
            "purchase_value_reason": _text(raw.get("new_purchase_value")),
            "selection_instruction": _text(raw.get("why_add")),
            "transition_from_previous": _text(order_row.get("why_now")),
        })

    plan_data = {
        "purchase_question_route": ordered_routes,
        "opening_package": dict(baseline_plan_data.get("opening_package") or {}),
        "purchase_cognition_path": ordered_paths,
        "chapters": ordered_chapters,
        "story_consumption": dict(baseline_plan_data.get("story_consumption") or {}),
        "duration_assessment": {
            "status": "journey_complete",
            "reason": "P0.3 单次叙事丰富度探索后的自然结束",
        },
    }
    combined_rank_by_id = {item.candidate_id: item for item in baseline_ranks}
    for ranks_for_chapter in (*existing_extra_ranks.values(), *new_ranks.values()):
        for item in ranks_for_chapter:
            combined_rank_by_id[item.candidate_id] = item
    combined_ranks = [
        combined_rank_by_id[candidate_id]
        for chapter_id in order_refs
        for candidate_id in expected_by_ref.get(chapter_id, ())
        if candidate_id in combined_rank_by_id
    ]
    # Rank numbers are presentation receipts only.  Preserve the existing
    # opening_rank=1 anchor; added material can never become a hidden Hook.
    combined_ranks = [replace(item, rank=index) for index, item in enumerate(combined_ranks, 1)]
    audit = {
        "stage": "p0_3_narrative_enrichment_single_pass",
        "triggered": True,
        "enrichment_status": status,
        "existing_chapter_enrichment": [dict(item) for item in existing_rows.values()],
        "new_structure_chapters": [dict(item) for item in new_rows.values()],
        "rejected_opportunities": _as_mapping_list(data.get("rejected_opportunities")),
        "final_order": final_order,
        "p0_2_quality_reaudit": quality,
        "baseline_candidate_ids": sorted(base_candidate_ids),
        "added_candidate_ids": sorted(selected_extra_ids),
        "errors": list(dict.fromkeys(errors)),
    }
    return audit, tuple(combined_ranks), plan_data, list(dict.fromkeys(errors))


def _packet_progression(
    *, raw: Mapping[str, Any], ordered_candidate_ids: tuple[int, ...],
    require_anchor_annotation: bool,
    errors: list[str], scope: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read an M2-declared micro narrative without inferring missing steps."""
    progression = _as_mapping_list(raw.get("progression"))
    progression_ids = tuple(
        (_as_int_tuple(item.get("candidate_id")) or (0,))[0]
        for item in progression
    )
    if progression_ids != ordered_candidate_ids:
        errors.append(f"chapter_packet_progression_candidate_order_mismatch:{scope}")
    seen_sub_values: set[str] = set()
    annotations: list[dict[str, Any]] = []
    for index, item in enumerate(progression):
        candidate_id = ( _as_int_tuple(item.get("candidate_id")) or (0,) )[0]
        function = _text(item.get("function")).lower()
        sub_value = _text(item.get("new_sub_value"))
        if not function or not sub_value:
            errors.append(f"chapter_packet_progression_detail_missing:{scope}:{candidate_id or index + 1}")
        elif sub_value in seen_sub_values:
            errors.append(f"chapter_packet_progression_sub_value_repeated:{scope}:{candidate_id}")
        seen_sub_values.add(sub_value)
        annotation = dict(item.get("candidate_annotation") or {})
        if index > 0 or require_anchor_annotation:
            if not annotation:
                errors.append(f"chapter_packet_progression_annotation_missing:{scope}:{candidate_id}")
            else:
                annotation["candidate_id"] = candidate_id
                annotations.append(annotation)
    return [dict(item) for item in progression], annotations


def _parse_chapter_packets(
    data: Mapping[str, Any], *, baseline_plan_data: Mapping[str, Any],
    baseline_path_audit: Mapping[str, Any], baseline_ranks: Sequence[StrongClipRank],
    remaining_tags: Sequence[CommerceLiteTag], source_windows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[StrongClipRank, ...], dict[str, Any], list[str]]:
    """Validate one M2 Packet declaration and project it into the M2 plan.

    This function deliberately delegates candidate/relation checks to the
    existing P0.3 parser.  P0.4 contributes only source-window provenance and
    packet-level information-progression requirements; it never selects a
    candidate when M2 omitted one.
    """
    errors: list[str] = []
    status = _text(data.get("packet_status")).lower()
    if status not in {"packet_enriched", "no_worthwhile_packet"}:
        errors.append("chapter_packet_status_invalid")
    base_chapters = {
        _text(item.get("chapter_id")): dict(item)
        for item in _as_mapping_list(baseline_plan_data.get("chapters"))
        if _text(item.get("chapter_id"))
    }
    base_candidate_ids = {
        candidate_id for item in base_chapters.values()
        for candidate_id in _as_int_tuple(item.get("candidate_ids"))
    }
    window_by_id = {
        _text(item.get("source_window_id")): dict(item)
        for item in source_windows if _text(item.get("source_window_id"))
    }
    expected_windows = set(window_by_id)
    raw_explored = data.get("explored_source_window_ids") or ()
    if isinstance(raw_explored, (str, bytes, bytearray)):
        raw_explored = (raw_explored,)
    explored_windows = list(dict.fromkeys(
        str(item or "").strip() for item in raw_explored if str(item or "").strip()
    ))
    if not expected_windows.issubset(set(explored_windows)):
        errors.append("chapter_packet_source_windows_not_all_explored")
    if any(item not in expected_windows for item in explored_windows):
        errors.append("chapter_packet_unknown_source_window_explored")

    packet_ids: set[str] = set()
    existing_rows: list[dict[str, Any]] = []
    packet_receipts: list[dict[str, Any]] = []
    for raw_value in _as_mapping_list(data.get("existing_chapter_packets")):
        raw = dict(raw_value)
        packet_id = _text(raw.get("packet_id"))
        chapter_id = _text(raw.get("chapter_id"))
        window_id = _text(raw.get("source_window_id"))
        if not packet_id or packet_id in packet_ids:
            errors.append(f"chapter_packet_id_missing_or_repeated:{packet_id or 'missing'}")
        packet_ids.add(packet_id)
        if _text(raw.get("packet_type")) != "existing_chapter_expansion" or chapter_id not in base_chapters:
            errors.append(f"chapter_packet_existing_chapter_invalid:{packet_id or chapter_id or 'missing'}")
        window = window_by_id.get(window_id)
        if not window or _text(window.get("kind")) != "existing_chapter_anchor":
            errors.append(f"chapter_packet_existing_source_window_invalid:{packet_id or chapter_id}")
        anchor_id = (_as_int_tuple(raw.get("anchor_candidate_id")) or (0,))[0]
        base_ids = _as_int_tuple(base_chapters.get(chapter_id, {}).get("candidate_ids"))
        if anchor_id not in base_ids or (window and anchor_id != int(window.get("anchor_candidate_id") or 0)):
            errors.append(f"chapter_packet_existing_anchor_invalid:{packet_id or chapter_id}:{anchor_id}")
        ordered_ids = _as_int_tuple(raw.get("ordered_candidate_ids"))
        support_ids = _as_int_tuple(raw.get("support_candidate_ids"))
        if not ordered_ids or ordered_ids[0] != anchor_id or ordered_ids[1:] != support_ids or len(ordered_ids) != len(set(ordered_ids)):
            errors.append(f"chapter_packet_existing_order_invalid:{packet_id or chapter_id}")
        if not support_ids or set(support_ids).intersection(base_candidate_ids):
            errors.append(f"chapter_packet_existing_support_missing_or_reused:{packet_id or chapter_id}")
        progression, annotations = _packet_progression(
            raw=raw, ordered_candidate_ids=ordered_ids, require_anchor_annotation=False,
            errors=errors, scope=packet_id or chapter_id,
        )
        eligible_ids = set(_as_int_tuple((window or {}).get("eligible_packet_candidate_ids")))
        off_window = set(support_ids) - eligible_ids
        if off_window and not _text(raw.get("cross_window_reason")):
            errors.append(f"chapter_packet_existing_cross_window_reason_missing:{packet_id or chapter_id}")
        required = ("chapter_intent", "purchase_value", "why_this_is_a_packet", "core_desire_compatibility", "net_improvement")
        if not all(_text(raw.get(key)) for key in required):
            errors.append(f"chapter_packet_existing_detail_missing:{packet_id or chapter_id}")
        existing_rows.append({
            "chapter_id": chapter_id,
            "current_value": _text(raw.get("purchase_value")),
            "candidate_ids": list(support_ids), "candidate_annotations": annotations,
            "new_sub_value": "；".join(_text(item.get("new_sub_value")) for item in progression[1:]),
            "why_add": _text(raw.get("why_this_is_a_packet")),
            "net_improvement": _text(raw.get("net_improvement")),
            # P0.4 does not use P0.3's sentence-count limit.  The M2 packet
            # progression/quality receipt owns whether a richer chapter drags.
            "support_count_explanation": "packet_information_progression_verified",
        })
        packet_receipts.append({
            "packet_id": packet_id, "packet_type": "existing_chapter_expansion", "chapter_id": chapter_id,
            "source_window": window, "anchor_candidate_id": anchor_id,
            "ordered_candidate_ids": list(ordered_ids), "support_candidate_ids": list(support_ids),
            "progression": progression, "cross_window_reason": _text(raw.get("cross_window_reason")),
            "purchase_value": _text(raw.get("purchase_value")), "chapter_intent": _text(raw.get("chapter_intent")),
        })

    new_rows: list[dict[str, Any]] = []
    for raw_value in _as_mapping_list(data.get("new_structure_packets")):
        raw = dict(raw_value)
        packet_id = _text(raw.get("packet_id"))
        chapter_id = _text(raw.get("enrichment_chapter_id"))
        window_id = _text(raw.get("source_window_id"))
        if not packet_id or packet_id in packet_ids:
            errors.append(f"chapter_packet_id_missing_or_repeated:{packet_id or 'missing'}")
        packet_ids.add(packet_id)
        window = window_by_id.get(window_id)
        if not window or _text(window.get("kind")) != "new_structure_discovery":
            errors.append(f"chapter_packet_new_source_window_invalid:{packet_id or chapter_id}")
        anchor_id = (_as_int_tuple(raw.get("anchor_candidate_id")) or (0,))[0]
        ordered_ids = _as_int_tuple(raw.get("ordered_candidate_ids"))
        candidate_ids = _as_int_tuple(raw.get("candidate_ids"))
        if not ordered_ids or ordered_ids[0] != anchor_id or candidate_ids != ordered_ids or len(ordered_ids) != len(set(ordered_ids)):
            errors.append(f"chapter_packet_new_order_invalid:{packet_id or chapter_id}")
        if set(candidate_ids).intersection(base_candidate_ids):
            errors.append(f"chapter_packet_new_candidate_reused:{packet_id or chapter_id}")
        progression, annotations = _packet_progression(
            raw=raw, ordered_candidate_ids=ordered_ids, require_anchor_annotation=True,
            errors=errors, scope=packet_id or chapter_id,
        )
        eligible_ids = set(_as_int_tuple((window or {}).get("eligible_packet_candidate_ids")))
        off_window = set(candidate_ids) - eligible_ids
        if off_window and not _text(raw.get("cross_window_reason")):
            errors.append(f"chapter_packet_new_cross_window_reason_missing:{packet_id or chapter_id}")
        required = (
            "chapter_intent", "purchase_value", "purchase_value_domain", "narrative_role",
            "recommended_insert_after", "core_desire_compatibility", "why_this_is_a_packet", "why_add", "net_improvement",
        )
        if not all(_text(raw.get(key)) for key in required) or not _as_text_tuple(raw.get("purchase_value_outcomes")):
            errors.append(f"chapter_packet_new_detail_missing:{packet_id or chapter_id}")
        new_rows.append({
            "enrichment_chapter_id": chapter_id, "chapter_intent": _text(raw.get("chapter_intent")),
            "new_purchase_value": _text(raw.get("purchase_value")), "candidate_ids": list(candidate_ids),
            "candidate_annotations": annotations, "purchase_value_domain": _text(raw.get("purchase_value_domain")),
            "purchase_value_outcomes": list(_as_text_tuple(raw.get("purchase_value_outcomes"))),
            "narrative_role": _text(raw.get("narrative_role")),
            "recommended_insert_after": _text(raw.get("recommended_insert_after")),
            "compatibility_with_core_desire": _text(raw.get("core_desire_compatibility")),
            "why_add": _text(raw.get("why_add")), "net_improvement": _text(raw.get("net_improvement")),
            "support_count_explanation": "packet_information_progression_verified",
        })
        packet_receipts.append({
            "packet_id": packet_id, "packet_type": "new_structure_chapter", "enrichment_chapter_id": chapter_id,
            "source_window": window, "anchor_candidate_id": anchor_id,
            "ordered_candidate_ids": list(ordered_ids), "progression": progression,
            "cross_window_reason": _text(raw.get("cross_window_reason")),
            "purchase_value": _text(raw.get("purchase_value")), "chapter_intent": _text(raw.get("chapter_intent")),
        })

    has_packets = bool(existing_rows or new_rows)
    if status == "packet_enriched" and not has_packets:
        errors.append("chapter_packet_enriched_without_packet")
    if status == "no_worthwhile_packet" and has_packets:
        errors.append("chapter_packet_no_worthwhile_with_packet")
    quality = dict(data.get("final_packet_quality") or {}) if isinstance(data.get("final_packet_quality"), Mapping) else {}
    quality_keys = (
        "whole_transcript_passed", "opening_unchanged", "natural_flow", "no_repeat", "no_drag", "ending_natural",
        "packet_internal_flow", "packet_information_progression", "packet_redundancy",
        "packet_to_previous_chapter_transition", "packet_to_next_chapter_transition", "whole_video_pacing",
    )
    for key in quality_keys:
        if not _as_bool(quality.get(key)):
            errors.append(f"chapter_packet_final_quality_not_passed:{key}")
    if not _text(quality.get("reason")):
        errors.append("chapter_packet_final_quality_reason_missing")

    p03_shape = {
        "existing_chapter_enrichment": existing_rows,
        "new_structure_chapters": new_rows,
        "rejected_opportunities": _as_mapping_list(data.get("rejected_source_windows")),
        "final_order": _as_mapping_list(data.get("final_order")),
        "enrichment_quality": {
            key: quality.get(key) for key in (
                "whole_transcript_passed", "opening_unchanged", "natural_flow", "no_repeat", "no_drag", "ending_natural",
            )
        } | {
            "reason": _text(quality.get("reason")),
            "dropped_new_candidate_ids": list(_as_int_tuple(quality.get("dropped_new_candidate_ids"))),
            "dropped_new_chapter_ids": [
                _text(item).replace("P_", "") for item in _as_text_tuple(quality.get("dropped_new_packet_ids"))
            ],
        },
        "enrichment_status": "enriched" if status == "packet_enriched" else "no_worthwhile_enrichment",
    }
    enrichment_audit, ranks, plan_data, enrichment_errors = _parse_narrative_enrichment(
        p03_shape, baseline_plan_data=baseline_plan_data, baseline_path_audit=baseline_path_audit,
        baseline_ranks=baseline_ranks, remaining_tags=remaining_tags,
        existing_candidate_limit=None, existing_exception_limit=None,
        new_candidate_limit=None, new_exception_limit=None,
    )
    errors.extend(enrichment_errors)
    packet_audit = {
        "stage": "p0_4_chapter_packet_builder_single_pass",
        "triggered": True, "packet_status": status,
        "source_windows": [dict(item) for item in source_windows],
        "explored_source_window_ids": explored_windows,
        "existing_chapter_packets": [
            item for item in packet_receipts
            if item.get("packet_type") == "existing_chapter_expansion"
        ],
        "new_structure_packets": [item for item in packet_receipts if item.get("packet_type") == "new_structure_chapter"],
        "rejected_source_windows": _as_mapping_list(data.get("rejected_source_windows")),
        "final_packet_quality": quality,
        "single_candidate_enrichment_recommended": _as_bool(data.get("single_candidate_enrichment_recommended")),
        "added_candidate_ids": list(enrichment_audit.get("added_candidate_ids") or ()),
        "errors": list(dict.fromkeys(errors)),
    }
    return packet_audit, ranks, plan_data, list(dict.fromkeys(errors))


def plan_commerce_lite_strong_clip_llm(
    *, strategy: Any, tags: Sequence[CommerceLiteTag], target_duration: float,
    safe_candidates: Sequence[PlanningCandidate], selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]], api_key: str, base_url: str, model: str,
    director_strategy_contract: Mapping[str, Any] | None = None,
    strong_ranking_response_hook: Callable[[str], None] | None = None,
    composition_response_hook: Callable[[str], None] | None = None,
    targeted_recall_response_hook: Callable[[str], None] | None = None,
    quality_response_hook: Callable[[str], None] | None = None,
    chapter_packet_response_hook: Callable[[str], None] | None = None,
    narrative_enrichment_response_hook: Callable[[str], None] | None = None,
    source_context_units: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[tuple[StrongClipRank, ...], NarrativePlan]:
    """Rank a core M2 path, then complete unanswered buyer questions in M2."""
    opening_scope = _director_opening_scope(director_strategy_contract)
    early_journey_scope = _director_early_journey_scope(director_strategy_contract)
    related = _strategy_related_tags(tags, strategy=strategy)
    if len(related) < 2:
        raise ValueError("Strong Clip Ranking 缺少至少两条当前策略关联候选")
    rank_response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_strong_clip_ranking_prompt(
            strategy=strategy, tags=related, director_strategy_contract=director_strategy_contract,
        ), stage=COMMERCE_STRONG_CLIP_RANKING_STAGE, max_tokens=4600,
    )
    rank_content = _text(rank_response.get("choices", [{}])[0].get("message", {}).get("content"))
    if not rank_content:
        raise RuntimeError("Strong Clip Ranking 返回空内容")
    if strong_ranking_response_hook:
        strong_ranking_response_hook(rank_content)
    rank_candidate_ids = {tag.candidate_id for tag in related}
    rank_hook_eligible_ids = {
        tag.candidate_id for tag in related
        if tag.hook_eligible and not _quality_final_utterance_reject_reason(tag.text)
    }
    try:
        ranked, rank_audit = _parse_strong_clip_ranking(
            _extract_json(rank_content),
            candidate_ids=rank_candidate_ids,
            hook_eligible_ids=rank_hook_eligible_ids,
            opening_scope=opening_scope,
        )
    except ValueError as error:
        # This is one bounded retry of the same Strong Clip Ranking decision.
        # M2, not code, re-ranks the exact same pool; code only names the
        # frozen opening-permission violation it must correct.
        retry_response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_strong_clip_ranking_prompt(
                strategy=strategy, tags=related,
                director_strategy_contract=director_strategy_contract,
                retry_reason=str(error),
            ),
            stage=COMMERCE_STRONG_CLIP_RANKING_STAGE, max_tokens=4600,
        )
        retry_content = _text(retry_response.get("choices", [{}])[0].get("message", {}).get("content"))
        if not retry_content:
            raise RuntimeError("Strong Clip Ranking Opening 合同重答返回空内容") from error
        if strong_ranking_response_hook:
            strong_ranking_response_hook(retry_content)
        ranked, rank_audit = _parse_strong_clip_ranking(
            _extract_json(retry_content),
            candidate_ids=rank_candidate_ids,
            hook_eligible_ids=rank_hook_eligible_ids,
            opening_scope=opening_scope,
        )
    # A scene story without a clean, scope-compliant scene/experience entry
    # is unavailable by design.  It must not silently inherit a global Q1
    # slimming hook and masquerade as a different Archetype.
    if (
        opening_scope.get("archetype") == "scene_immersion"
        and not bool(rank_audit.get("opening_scope_available"))
    ):
        unavailable_contract = dict(selection_contract or {})
        unavailable_contract["narrative_archetype_availability"] = {
            "archetype": "scene_immersion",
            "status": "unavailable",
            "reason": "no_clean_scope_compliant_scene_or_experience_opening",
            "opening_scope": dict(opening_scope),
        }
        return ranked, NarrativePlan(
            strategy_id=_text(getattr(strategy, "strategy_id", "")),
            thesis=_text(getattr(strategy, "thesis", "")),
            target_duration=float(target_duration), beats=(),
            status="narrative_archetype_unavailable", recommended_duration=0.0,
            issues=("scene_immersion_unavailable:no_clean_scope_compliant_scene_or_experience_opening",),
            removed_beats=(), plan_valid=False,
            story_brief=CommercialStoryBrief.from_strategy(strategy),
            selection_contract=unavailable_contract,
            duration_assessment={
                "commerce_strong_clip_ranking": {
                    "stage": "strong_clip_ranking_archetype_opening_scope_p0_1", **rank_audit,
                },
                "commerce_purchase_journey": {
                    "journey_status": "narrative_archetype_unavailable",
                    "reason": "scene_immersion needs a clean Q6/Q4 opening; global Q1 fallback is forbidden",
                },
            },
        )
    # The core composition deliberately sees only the comparative ranking. If
    # it leaves buyer questions unanswered, the later targeted recall below is
    # the one controlled path back to the full safe pool.
    compose_response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_purchase_cognition_composition_prompt(
            strategy=strategy, ranked=ranked, tags=related, target_duration=target_duration,
            director_strategy_contract=director_strategy_contract,
        ), stage=COMMERCE_NARRATIVE_COMPOSITION_STAGE, max_tokens=4600,
    )
    compose_content = _text(compose_response.get("choices", [{}])[0].get("message", {}).get("content"))
    if not compose_content:
        raise RuntimeError("Narrative Composition 返回空内容")
    if composition_response_hook:
        composition_response_hook(compose_content)
    data = _extract_json(compose_content)
    contract = dict(selection_contract or {})
    # Do not turn the retired Lite editor/budget gates into a second director.
    # A bridge that adds no new purchase value must also not invalidate an
    # otherwise faithful M2→M3 path.
    for key in (
        "commerce_lite_purchase_value_progression", "commerce_lite_story_budget",
        "commerce_lite_chapter_saturation", "commerce_lite_final_editor_experiment",
        "m1_consumption_validation_require_supporting_bridge",
    ):
        contract.pop(key, None)
    contract.update({
        "commerce_strong_clip_ranking_then_cognition_composition": True,
        "commerce_purchase_journey_stop_rules": {
            "max_evidence_per_purchase_outcome": 3,
            "max_same_evidence_function_per_purchase_outcome": 1,
            "max_same_purchase_question_answer_role_outcome": 1,
            "new_answered_question_required_for_each_chapter": True,
            "opening_uses_independent_opening_rank_one": True,
            "full_safe_pool_targeted_recall_before_source_insufficiency": True,
        },
        "ranked_candidate_ids": [item.candidate_id for item in ranked],
        "ranked_candidate_pool_size": len(ranked),
        "target_duration_is_soft": True,
        "natural_ending_allowed": True,
        # M1 remains immutable source lineage.  It is not the final narration
        # brief once the Director has selected an Archetype-specific intent.
        "m1_source_story": _compact_story(strategy),
        "director_narrative_contract": {
            "authority": "director_strategy_contract",
            "narrative_archetype": _text((director_strategy_contract or {}).get("narrative_archetype")),
            "core_desire": _text((director_strategy_contract or {}).get("core_desire")),
            "opening_promise": _text((director_strategy_contract or {}).get("opening_promise")),
            "opening_scope": dict((director_strategy_contract or {}).get("opening_scope") or {}),
            "early_journey_scope": dict((director_strategy_contract or {}).get("early_journey_scope") or {}),
            "source_lineage": "m1_source_story",
        },
    })
    annotated = bind_story_assets(CommercialStoryBrief.from_strategy(strategy), tuple(safe_candidates))

    def validate_composition(
        plan_data: Mapping[str, Any], *, allowed_ranks: Sequence[StrongClipRank],
    ) -> tuple[NarrativePlan, dict[str, Any], list[int]]:
        raw_beats = _parse_beats(plan_data)
        allowed_ids = {item.candidate_id for item in allowed_ranks}
        candidate_ids = [candidate_id for beat in raw_beats for candidate_id in beat.candidate_ids]
        unknown = sorted(set(candidate_ids) - allowed_ids)
        if unknown:
            raise ValueError(
                "Purchase Journey Composition 引用了未获 M2 关系许可的候选："
                + ",".join(map(str, unknown))
            )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Purchase Journey Composition 重复使用 candidate_id")
        plan = NarrativePlan(
            strategy_id=_text(getattr(strategy, "strategy_id", "")),
            thesis=_text(getattr(strategy, "thesis", "")),
            target_duration=float(target_duration),
            beats=_with_measured_beat_seconds(raw_beats, safe_candidates),
            status="insufficient_material", recommended_duration=0.0, issues=(), removed_beats=(), plan_valid=True,
            story_brief=CommercialStoryBrief.from_strategy(strategy),
            opening_package=_parse_opening_package(plan_data.get("opening_package")),
            selection_contract=contract,
            duration_assessment=_parse_duration_assessment(plan_data.get("duration_assessment")),
            duration_plan=_parse_duration_plan(plan_data.get("duration_plan"), target_duration=float(target_duration)),
            depth_expansion=_parse_depth_expansion(plan_data.get("depth_expansion"), target_duration=float(target_duration)),
            story_consumption=_parse_story_consumption(plan_data.get("story_consumption")),
        )
        validated_plan = validate_narrative_plan(
            plan, annotated, executable_evidence=executable_evidence,
        )
        return (
            validated_plan,
            _purchase_path_audit(plan_data, beats=validated_plan.beats, ranked=allowed_ranks),
            candidate_ids,
        )

    validated, path_audit, selected_ids = validate_composition(data, allowed_ranks=ranked)
    initial_path_audit = dict(path_audit)
    initial_selected_ids = list(selected_ids)
    final_data: Mapping[str, Any] = data
    final_ranked: tuple[StrongClipRank, ...] = ranked
    journey_errors: list[str] = []
    narrative_depth = _narrative_depth_blueprint(
        director_strategy_contract, target_duration=target_duration,
    )

    def missing_requirements(audit: Mapping[str, Any]) -> list[dict[str, Any]]:
        return (
            _blueprint_missing_purchase_questions(audit, narrative_depth)
            if narrative_depth is not None else _missing_purchase_questions(audit)
        )

    missing_questions = missing_requirements(path_audit)
    unconsumed_tags = _targeted_recall_candidates(tags, selected_ids=set(selected_ids))
    journey_audit: dict[str, Any] = {
        "stage": (
            "purchase_journey_narrative_blueprint_then_full_pool_targeted_recall_p0"
            if narrative_depth is not None else
            "purchase_journey_missing_detection_then_full_pool_targeted_recall_v1"
        ),
        "initial_actual_seconds": validated.total_seconds,
        "initial_selected_candidate_ids": initial_selected_ids,
        "initial_path_passed": bool(path_audit.get("passed")),
        "initial_missing_purchase_questions": _recall_question_payload(missing_questions),
        "full_executable_safe_candidate_pool_size": sum(1 for tag in tags if tag.materializable),
        "unconsumed_full_executable_safe_candidate_pool_size": len(unconsumed_tags),
        "targeted_recall": None,
        "narrative_depth": {
            "enabled": narrative_depth is not None,
            "activation_range_seconds": [NARRATIVE_DEPTH_TARGET_MIN_SECONDS, NARRATIVE_DEPTH_TARGET_MAX_SECONDS],
            "target_duration": float(target_duration),
            "blueprint": narrative_depth,
            "initial_missing_chapter_slots": _recall_question_payload(missing_questions) if narrative_depth else [],
            "final_missing_chapter_slots": [],
            "stop_rule": (
                "no_unexplored_high_value_slot_with_clean_source_evidence"
                if narrative_depth else "ordinary_purchase_journey_completion"
            ),
        },
    }
    journey_status = "journey_complete" if not missing_questions else "journey_incomplete"
    if not path_audit["passed"]:
        # The selected IDs are already safe and executable at this point.  A
        # bad question-role declaration is a quality-editing defect, not a
        # reason to abandon the full-pool journey recall; the final M2 quality
        # decision below must repair it before M3 can run.
        journey_audit["initial_path_errors"] = list(path_audit.get("errors") or ())
    if missing_questions and not unconsumed_tags:
        journey_status = "source_material_insufficient"
        journey_audit["targeted_recall"] = {
            "skipped": "no_unconsumed_candidate_after_initial_core_path",
            "missing_purchase_questions": journey_audit["initial_missing_purchase_questions"],
            "recall_by_question": [],
        }
    elif missing_questions:
        recall_response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_purchase_journey_targeted_recall_prompt(
                strategy=strategy, director_strategy_contract=director_strategy_contract,
                target_duration=target_duration, initial_path_audit=path_audit,
                missing_questions=missing_questions, unconsumed_tags=unconsumed_tags,
            ),
            stage=COMMERCE_PURCHASE_JOURNEY_RECALL_STAGE, max_tokens=4600,
        )
        recall_content = _text(recall_response.get("choices", [{}])[0].get("message", {}).get("content"))
        if not recall_content:
            raise RuntimeError("Purchase Journey Targeted Recall 返回空内容")
        if targeted_recall_response_hook:
            targeted_recall_response_hook(recall_content)
        recall_data = _extract_json(recall_content)
        recall_audit, recalled_ranks, recall_errors = _parse_purchase_journey_targeted_recall(
            recall_data, missing_questions=missing_questions, unconsumed_tags=unconsumed_tags,
        )
        journey_audit["targeted_recall"] = recall_audit
        journey_errors.extend(recall_errors)
        selected_recall_ids = set(recall_audit["selected_candidate_ids"])
        if not recall_errors and selected_recall_ids:
            merged_data, merge_errors = _merge_purchase_journey_continuation(
                data, recall_data, selected_ids=selected_recall_ids,
            )
            journey_errors.extend(merge_errors)
            if not merge_errors:
                final_data = merged_data
                final_ranked = tuple(ranked) + tuple(recalled_ranks)
                validated, path_audit, selected_ids = validate_composition(
                    final_data, allowed_ranks=final_ranked,
                )
        elif not recall_errors and not selected_recall_ids:
            journey_status = "source_material_insufficient"

        remaining_questions = missing_requirements(path_audit)
        recalled_by_question = {
            _text(item.get("purchase_question_id")): item
            for item in (journey_audit.get("targeted_recall") or {}).get("recall_by_question") or ()
            if isinstance(item, Mapping)
        }
        remaining_with_real_value = [
            item for item in remaining_questions
            if (recalled_by_question.get(str(item["purchase_question_id"])) or {}).get("recall_candidates")
        ]
        if journey_errors or remaining_with_real_value:
            journey_status = "journey_incomplete"
            if remaining_with_real_value:
                journey_errors.append("purchase_journey_new_value_not_completed")
        elif remaining_questions:
            journey_status = "source_material_insufficient"
        else:
            journey_status = "journey_complete"
    final_missing_questions = missing_requirements(path_audit)
    journey_audit["final_missing_purchase_questions"] = _recall_question_payload(final_missing_questions)
    if narrative_depth is not None:
        journey_audit["narrative_depth"]["final_missing_chapter_slots"] = _recall_question_payload(final_missing_questions)
    journey_audit["final_selected_candidate_ids"] = list(selected_ids)
    journey_audit["final_actual_seconds"] = validated.total_seconds
    journey_audit["journey_status"] = journey_status
    journey_audit["errors"] = list(dict.fromkeys(journey_errors))

    # Journey Completion above is deliberately left intact: it found the
    # available buyer questions.  This final M2 operation is only allowed to
    # replace weak wording, reorder retained questions, or delete an optional
    # weak question.  It cannot introduce a new buyer question or pad time.
    quality_audit: dict[str, Any] | None = None
    # Recall may have found new value but produced a bad declared dependency
    # (for example treating Q5 as proof of Q4).  Quality is the one allowed
    # M2 repair for that selected journey; it must be able to replace the
    # malformed sequence before the common validator sees the final plan.
    if not journey_errors:
        completion_selected_ids = list(selected_ids)
        completion_seconds = validated.total_seconds
        completion_status = journey_status
        current_question_ids = [
            _text(row.get("purchase_question_id"))
            for row in path_audit.get("steps") or ()
            if isinstance(row, Mapping) and _text(row.get("purchase_question_id"))
        ]
        # The first M2 pass already gave every present buyer question a
        # semantic direction.  Quality may replace the weak line, but must
        # not silently turn “why does it look slim?” into a different fabric
        # story just because that M1 theme is also available.  This is copied
        # from M2's own declared relation; it does not classify or pick a
        # candidate in code.
        existing_answer_anchors: dict[str, dict[str, Any]] = {}
        for relation in path_audit.get("candidate_relations") or ():
            if not isinstance(relation, Mapping):
                continue
            question_id = _text(relation.get("purchase_question_id"))
            if question_id not in current_question_ids:
                continue
            anchor = existing_answer_anchors.setdefault(question_id, {
                "purchase_question_id": question_id,
                "existing_candidate_ids": [],
                "existing_purchase_outcomes": [],
                "existing_answer_roles": [],
            })
            anchor["existing_candidate_ids"].extend(_as_int_tuple(relation.get("candidate_id")))
            for field, target in (
                ("purchase_outcome", "existing_purchase_outcomes"),
                ("answer_role", "existing_answer_roles"),
            ):
                value = _text(relation.get(field))
                if value:
                    anchor[target].append(value)
        for anchor in existing_answer_anchors.values():
            for key in ("existing_candidate_ids", "existing_purchase_outcomes", "existing_answer_roles"):
                anchor[key] = list(dict.fromkeys(anchor[key]))
        local_quality_rows: list[dict[str, Any]] = []
        local_quality_responses: list[dict[str, Any]] = []
        local_quality_retries: list[dict[str, Any]] = []
        locally_consumed_candidate_ids: set[int] = set()
        # Q2 is ranked first only to reserve one clean mechanism/proof for
        # the mandatory explanation step.  The final director order remains
        # Q1 -> Q2; this is a relationship constraint, not a programmatic
        # candidate choice or a new planning layer.
        quality_scope = (
            dict(narrative_depth.get("early_journey_scope") or {})
            if narrative_depth is not None else dict(early_journey_scope)
        )
        quality_scope = _director_early_journey_scope({"early_journey_scope": quality_scope})
        quality_required_ids = tuple(quality_scope["required_question_ids"])
        quality_recommended_ids = tuple(quality_scope["recommended_question_ids"])
        quality_optional_ids = tuple(quality_scope["optional_question_ids"])
        questions_by_id = {
            _text(item.get("purchase_question_id")): item
            for item in _purchase_journey_question_payload(current_question_ids)
        }
        quality_questions = [
            questions_by_id[question_id]
            for question_id in quality_scope["preferred_question_order"]
            if question_id in questions_by_id
        ]
        quality_questions.extend(
            question for question_id, question in questions_by_id.items()
            if question_id not in {_text(item.get("purchase_question_id")) for item in quality_questions}
        )
        depth_priority_by_question = {
            _text(slot.get("purchase_question_id")): int(_number(slot.get("priority")) or 99)
            for slot in (narrative_depth or {}).get("chapter_slots") or ()
            if isinstance(slot, Mapping) and _text(slot.get("purchase_question_id"))
            and _text(slot.get("coverage")).lower() != "optional"
        }
        local_selection_questions = sorted(
            quality_questions,
            key=lambda item: (
                quality_scope["preferred_question_order"].index(_text(item.get("purchase_question_id")))
                if _text(item.get("purchase_question_id")) in quality_scope["preferred_question_order"]
                else 90 + depth_priority_by_question.get(_text(item.get("purchase_question_id")), 90)
            ),
        )
        local_rows_by_question: dict[str, dict[str, Any]] = {}
        for question in local_selection_questions:
            local_response = _post_lite_request(
                api_key=api_key, base_url=base_url, model=model,
                prompt=build_purchase_question_local_quality_prompt(
                    question=question, tags=tags,
                    existing_answer_anchor=existing_answer_anchors.get(_text(question.get("purchase_question_id"))),
                    excluded_candidate_ids=tuple(sorted(locally_consumed_candidate_ids)),
                    narrative_depth=narrative_depth,
                    early_journey_scope=quality_scope,
                ),
                stage=COMMERCE_PURCHASE_QUESTION_LOCAL_QUALITY_STAGE, max_tokens=1600,
            )
            local_content = _text(local_response.get("choices", [{}])[0].get("message", {}).get("content"))
            if not local_content:
                journey_errors.append("purchase_question_local_quality_response_empty:" + _text(question.get("purchase_question_id")))
                continue
            local_row = _extract_json(local_content)
            if not isinstance(local_row, Mapping):
                journey_errors.append("purchase_question_local_quality_response_invalid:" + _text(question.get("purchase_question_id")))
                continue
            local_rows_by_question[_text(question.get("purchase_question_id"))] = dict(local_row)
            declared_selection = _quality_selected_candidate_ids(local_row)
            if declared_selection:
                # Each later question still sees every *unconsumed* safe
                # candidate. This is only a no-reuse relationship contract,
                # never a semantic shortlist or program choice.
                locally_consumed_candidate_ids.update(declared_selection)
        # A malformed local M2 answer must not make code choose a substitute.
        # It receives one bounded retry of the same local Quality decision,
        # with the exact formal relation violation and the same full legal
        # pool minus candidates already reserved by other questions.
        if narrative_depth is not None:
            for question in local_selection_questions:
                question_id = _text(question.get("purchase_question_id"))
                original_row = local_rows_by_question.get(question_id)
                if not isinstance(original_row, Mapping):
                    continue
                local_errors = _quality_local_selection_contract_errors(
                    original_row, tags=tags, required_question_ids=quality_required_ids,
                )
                if not local_errors:
                    continue
                occupied_ids = {
                    candidate_id
                    for other_question_id, other_row in local_rows_by_question.items()
                    if other_question_id != question_id
                    for candidate_id in _quality_selected_candidate_ids(other_row)
                }
                retry_response = _post_lite_request(
                    api_key=api_key, base_url=base_url, model=model,
                    prompt=build_purchase_question_local_quality_prompt(
                        question=question, tags=tags,
                        existing_answer_anchor=existing_answer_anchors.get(question_id),
                        excluded_candidate_ids=tuple(sorted(occupied_ids)),
                        narrative_depth=narrative_depth,
                        early_journey_scope=quality_scope,
                        retry_reason="；".join(local_errors),
                    ),
                    stage=COMMERCE_PURCHASE_QUESTION_LOCAL_QUALITY_STAGE, max_tokens=1600,
                )
                retry_content = _text(retry_response.get("choices", [{}])[0].get("message", {}).get("content"))
                retry_row = _extract_json(retry_content) if retry_content else None
                if not isinstance(retry_row, Mapping):
                    journey_errors.append("purchase_question_local_quality_retry_invalid:" + question_id)
                    continue
                local_rows_by_question[question_id] = dict(retry_row)
                local_quality_retries.append({
                    "purchase_question_id": question_id,
                    "initial_errors": list(local_errors),
                    "retry_row": dict(retry_row),
                })
        # Keep every following M2 prompt and the public audit in formal buyer
        # journey order even though Q2 was locally compared first.
        local_quality_rows = [
            local_rows_by_question[_text(question.get("purchase_question_id"))]
            for question in quality_questions
            if _text(question.get("purchase_question_id")) in local_rows_by_question
        ]
        local_quality_responses = list(local_quality_rows)
        # This is bounded prompt context, not a shortlist: M2 has already
        # declared every local comparison candidate.  Supplying their literal
        # duration lets the same quality-order decision justify a legitimate
        # 5–8s Hook micro-sequence without code inventing that judgement.
        quality_candidate_ids = {
            candidate_id
            for row in local_quality_rows
            for candidate_id in (
                (_as_int_tuple(item.get("candidate_id")) or (0,))[0]
                for item in _as_mapping_list(row.get("local_candidates"))
            )
            if candidate_id
        }
        quality_candidate_context = {
            tag.candidate_id: {
                "text": tag.text,
                "duration_seconds": round(float(tag.duration), 3),
            }
            for tag in tags
            if tag.materializable and tag.candidate_id in quality_candidate_ids
        }
        if not journey_errors:
            quality_order_attempts: list[dict[str, Any]] = []
            order_response = _post_lite_request(
                api_key=api_key, base_url=base_url, model=model,
                prompt=build_purchase_journey_quality_order_prompt(
                    strategy=strategy, local_quality_rows=local_quality_rows,
                    narrative_depth=narrative_depth,
                    early_journey_scope=quality_scope,
                    candidate_text_by_id=quality_candidate_context,
                ),
                stage=COMMERCE_PURCHASE_JOURNEY_QUALITY_STAGE, max_tokens=2600,
            )
            order_content = _text(order_response.get("choices", [{}])[0].get("message", {}).get("content"))
            if not order_content:
                journey_errors.append("purchase_journey_quality_order_response_empty")
            else:
                order_data = _extract_json(order_content)
                if not isinstance(order_data, Mapping):
                    journey_errors.append("purchase_journey_quality_order_response_invalid")
                else:
                    quality_order_attempts.append(dict(order_data))
                    selected_by_question, candidate_sequence_errors = _quality_order_candidate_sequence_contract(
                        order_data, local_quality_rows,
                    )
                    retained_ids = {
                        _text(item) for item in order_data.get("retained_question_ids") or () if _text(item)
                    }
                    strong_optional_ids = _strong_optional_quality_question_ids(
                        local_quality_rows,
                        required_question_ids=quality_required_ids,
                        recommended_question_ids=quality_recommended_ids,
                    )
                    dropped_strong_ids = [
                        question_id for question_id in strong_optional_ids
                        if question_id not in retained_ids
                    ]
                    opening_contract_errors = _quality_order_opening_contract_errors(
                        order_data, local_quality_rows,
                        opening_question_ids=quality_scope["opening_question_ids"],
                        selected_by_question_override=selected_by_question,
                        tags=tags,
                    )
                    journey_order_errors = _quality_order_journey_contract_errors(
                        order_data, narrative_depth=narrative_depth,
                        early_journey_scope=quality_scope,
                    )
                    literal_redundancy_errors = _quality_order_literal_redundancy_contract_errors(
                        order_data, tags,
                    )
                    flow_errors = _quality_order_flow_contract_errors(order_data)
                    if dropped_strong_ids or opening_contract_errors or journey_order_errors or candidate_sequence_errors or literal_redundancy_errors or flow_errors:
                        # This is a bounded M2 retry of the *same* ordering
                        # decision.  It sees the same locally AI-selected
                        # candidates and only the declared contract breach;
                        # code does not decide their semantic placement.
                        retry_response = _post_lite_request(
                            api_key=api_key, base_url=base_url, model=model,
                            prompt=build_purchase_journey_quality_order_prompt(
                                strategy=strategy, local_quality_rows=local_quality_rows,
                                required_question_ids=strong_optional_ids,
                                narrative_depth=narrative_depth,
                                early_journey_scope=quality_scope,
                                candidate_text_by_id=quality_candidate_context,
                                retry_reason=(
                                    (
                                        "上一版错误删除了已被局部排序判定为强、独立、口播干净的新购买问题："
                                        + ",".join(dropped_strong_ids)
                                        + (
                                            "。按当前 Archetype 的 preferred_question_order 保留："
                                            + "→".join(quality_scope["preferred_question_order"])
                                        )
                                        if dropped_strong_ids else ""
                                    )
                                    + (
                                        " Opening 合同错误："
                                        + ",".join(opening_contract_errors)
                                        + "。hook/payoff 必须严格是当前 Archetype opening_question_ids："
                                        + "→".join(quality_scope["opening_question_ids"])
                                        + "；不得使用后续章节。"
                                        + (
                                            " 若包含 purchase_quality_opening_hook_integrity_reason_missing，"
                                            "请根据给出的真实秒数补齐 hook_integrity_reason，具体说明保留的两句分别缺失会损失什么；"
                                            "不能只重复 hook_promise。"
                                            if "purchase_quality_opening_hook_integrity_reason_missing" in opening_contract_errors else ""
                                        )
                                        if opening_contract_errors else ""
                                    )
                                    + (
                                        " 购买旅程顺序错误："
                                        + ",".join(journey_order_errors)
                                        + "。必须遵循当前 Archetype preferred_question_order；Q7 仅可作为最后的自然收束。"
                                        if journey_order_errors else ""
                                    )
                                    + (
                                        " 同题候选只能保留原 anchor 加原序 support 子序列："
                                        + ",".join(candidate_sequence_errors)
                                        + "。不得换候选。"
                                        if candidate_sequence_errors else ""
                                    )
                                    + (
                                        " 实际原话已重复已有购买事实："
                                        + ",".join(literal_redundancy_errors)
                                        + "。不得用已被当前 anchor 或后续保留问题完整回答的同一字面事实做 support；"
                                        + "只能从原 selected_candidate_ids 删除该 support，不能换候选或新增内容。"
                                        if literal_redundancy_errors else ""
                                    )
                                    + (
                                        " 连读质量错误："
                                        + ",".join(flow_errors)
                                        + "。请根据上一句→当前句→下一句的真实原话删除重复/残句或重排，所有保留行必须 natural。"
                                        if flow_errors else ""
                                    )
                                ),
                            ),
                            stage=COMMERCE_PURCHASE_JOURNEY_QUALITY_STAGE, max_tokens=2600,
                        )
                        retry_content = _text(retry_response.get("choices", [{}])[0].get("message", {}).get("content"))
                        retry_data = _extract_json(retry_content) if retry_content else None
                        if not isinstance(retry_data, Mapping):
                            journey_errors.append("purchase_journey_quality_order_retry_invalid")
                        else:
                            order_data = dict(retry_data)
                            quality_order_attempts.append(dict(order_data))
                            selected_by_question, candidate_sequence_errors = _quality_order_candidate_sequence_contract(
                                order_data, local_quality_rows,
                            )
                            retained_ids = {
                                _text(item) for item in order_data.get("retained_question_ids") or () if _text(item)
                            }
                            dropped_strong_ids = [
                                question_id for question_id in strong_optional_ids
                                if question_id not in retained_ids
                            ]
                            opening_contract_errors = _quality_order_opening_contract_errors(
                                order_data, local_quality_rows,
                                opening_question_ids=quality_scope["opening_question_ids"],
                                selected_by_question_override=selected_by_question,
                                tags=tags,
                            )
                            journey_order_errors = _quality_order_journey_contract_errors(
                                order_data, narrative_depth=narrative_depth,
                                early_journey_scope=quality_scope,
                            )
                            literal_redundancy_errors = _quality_order_literal_redundancy_contract_errors(
                                order_data, tags,
                            )
                            flow_errors = _quality_order_flow_contract_errors(order_data)
                    if dropped_strong_ids:
                        # No semantic candidate is added here.  The order
                        # response is simply not allowed to erase an answer
                        # that its own local director has already called a
                        # clean, strong, independent new purchase value.
                        journey_errors.extend(
                            f"purchase_quality_strong_optional_question_dropped:{question_id}"
                            for question_id in dropped_strong_ids
                        )
                    journey_errors.extend(opening_contract_errors)
                    journey_errors.extend(journey_order_errors)
                    journey_errors.extend(candidate_sequence_errors)
                    journey_errors.extend(literal_redundancy_errors)
                    journey_errors.extend(flow_errors)
                    materialized_rows = []
                    for row in local_quality_rows:
                        item = dict(row)
                        if _text(item.get("purchase_question_id")) not in retained_ids:
                            # The director chose deletion.  This is a
                            # structural materialization of its decision, not
                            # a program-selected semantic candidate.
                            item["selected_candidate_ids"] = []
                            item["selected_candidate_id"] = 0
                        else:
                            # The AI ordering pass may remove a redundant
                            # support, but it cannot replace the local anchor
                            # or fabricate another utterance.  This only
                            # projects that declared subset into M2's schema.
                            selected_sequence = selected_by_question.get(
                                _text(item.get("purchase_question_id")),
                                _quality_selected_candidate_ids(item),
                            )
                            item["selected_candidate_ids"] = list(selected_sequence)
                            item["selected_candidate_id"] = selected_sequence[0] if selected_sequence else 0
                        materialized_rows.append(item)
                    quality_data = {**dict(order_data), "quality_by_question": materialized_rows}
                    if quality_response_hook:
                        quality_response_hook(json.dumps({
                            "local_quality_responses": local_quality_responses,
                            "local_quality_retries": local_quality_retries,
                            "quality_order_response": dict(order_data),
                            "quality_order_attempts": quality_order_attempts,
                            "strong_optional_question_ids": list(strong_optional_ids),
                            "director_dropped_strong_question_ids": dropped_strong_ids,
                            "opening_contract_errors": list(opening_contract_errors),
                            "journey_order_errors": list(journey_order_errors),
                            "candidate_sequence_errors": list(candidate_sequence_errors),
                            "literal_redundancy_errors": list(literal_redundancy_errors),
                            "flow_errors": list(flow_errors),
                            "materialized_quality_data": quality_data,
                        }, ensure_ascii=False, indent=2))
                    quality_audit, quality_ranks, quality_plan_data, quality_errors = _parse_purchase_journey_quality(
                        quality_data, current_question_ids=current_question_ids, tags=tags,
                        required_question_ids=quality_required_ids,
                        optional_question_ids=quality_optional_ids,
                        opening_scope=(narrative_depth or {}).get("opening_scope") or opening_scope,
                        early_journey_scope=quality_scope,
                    )
                    journey_errors.extend(quality_errors)
                    if not quality_errors:
                        quality_validated, quality_path_audit, quality_selected_ids = validate_composition(
                            quality_plan_data, allowed_ranks=quality_ranks,
                        )
                        if quality_path_audit.get("passed"):
                            validated = quality_validated
                            path_audit = quality_path_audit
                            selected_ids = quality_selected_ids
                            final_data = quality_plan_data
                            final_ranked = quality_ranks
                            journey_audit.update({
                                "completion_selected_candidate_ids": completion_selected_ids,
                                "completion_actual_seconds": completion_seconds,
                                "completion_journey_status": completion_status,
                                "quality_retained_question_ids": quality_audit["retained_question_ids"],
                                "quality_dropped_optional_question_ids": quality_audit["dropped_optional_question_ids"],
                                "final_selected_candidate_ids": list(selected_ids),
                                "final_actual_seconds": validated.total_seconds,
                            })
                            # The Archetype declares its own non-negotiable
                            # early journey; this does not change M2's local
                            # selection or let code add a candidate.
                            retained = set(quality_audit["retained_question_ids"])
                            if set(quality_required_ids).issubset(retained):
                                journey_status = "journey_complete"
                        else:
                            journey_errors.append("purchase_journey_quality_path_invalid")
                            quality_audit["path_errors"] = list(quality_path_audit.get("errors") or ())
                    if quality_audit is not None:
                        quality_audit["errors"] = list(dict.fromkeys(quality_errors))
    journey_audit["journey_status"] = journey_status
    journey_audit["errors"] = list(dict.fromkeys(journey_errors))

    opening = validated.opening_package
    opening_ids = tuple(opening.hook_candidate_ids) if opening else ()
    rank_by_id = {item.candidate_id: item for item in final_ranked}
    top_opening_ids = [
        item.candidate_id for item in final_ranked if item.opening_rank == 1
    ]
    invalid_opening_ids = [
        candidate_id for candidate_id in opening_ids
        if candidate_id not in rank_by_id
        or rank_by_id[candidate_id].fragment
        or rank_by_id[candidate_id].visual_dependency
    ]
    if invalid_opening_ids:
        path_audit["errors"].append(
            "opening_not_independent_strong_clip:" + ",".join(map(str, invalid_opening_ids))
        )
        path_audit["passed"] = False
    if len(top_opening_ids) != 1:
        path_audit["errors"].append("opening_rank_one_missing_or_ambiguous")
        path_audit["passed"] = False
    elif not opening_ids or opening_ids[0] != top_opening_ids[0]:
        path_audit["errors"].append(
            "opening_not_top_ranked_independent_candidate:"
            + str(top_opening_ids[0])
        )
        path_audit["passed"] = False

    # P0.4 begins from the already approved P0.2 cut.  It restores local source
    # context around each chapter anchor and builds dense remaining windows;
    # neither operation chooses a line or interprets a selling point.
    preferred_low = round(max(1.0, float(target_duration)) * 0.78, 3)
    pre_enrichment_errors = list(dict.fromkeys(
        list(path_audit.get("errors") or ()) + list(journey_errors)
    ))
    enrichment_remaining_tags = tuple(
        tag for tag in _targeted_recall_candidates(tags, selected_ids=set(selected_ids))
        if not _quality_final_utterance_reject_reason(tag.text)
    )
    packet_windows, packet_window_audit = build_chapter_packet_source_windows(
        completed_plan_data=final_data, tags=tags, safe_candidates=safe_candidates,
        executable_evidence=executable_evidence, source_context_units=source_context_units,
    )
    packet_audit: dict[str, Any] = {
        "stage": "p0_4_chapter_packet_builder_single_pass",
        "triggered": False,
        "baseline_actual_seconds": validated.total_seconds,
        "baseline_selected_candidate_ids": list(selected_ids),
        "source_window_preparation": packet_window_audit,
        "trigger_conditions": {
            "journey_complete": journey_status == "journey_complete",
            "p0_2_quality_passed": bool(quality_audit is not None and not pre_enrichment_errors and validated.plan_valid),
            "natural_complete_below_target": bool(validated.total_seconds < preferred_low),
            "narrative_depth_45_to_75": narrative_depth is not None,
            "narrative_blueprint_p0_1": _text((narrative_depth or {}).get("blueprint_version")) == "narrative-blueprint-p0.1",
            "remaining_safe_executable": bool(enrichment_remaining_tags),
            "source_windows_available": bool(packet_windows),
        },
    }
    packet_applied = False
    packet_trigger = all(packet_audit["trigger_conditions"].values())
    if not packet_trigger:
        packet_audit["skip_reason"] = "P0.4 only runs after P0.2 natural completion in a P0.1 45–75s narrative with usable local source windows"
    else:
        packet_audit["triggered"] = True
        packet_response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_chapter_packet_prompt(
                strategy=strategy, director_strategy_contract=dict(director_strategy_contract or {}),
                narrative_depth=narrative_depth or {}, completed_plan_data=final_data,
                completed_path_audit=path_audit, completed_ranks=final_ranked,
                tags=tags, source_windows=packet_windows, target_duration=target_duration,
            ),
            stage=COMMERCE_CHAPTER_PACKET_STAGE, max_tokens=6200,
        )
        packet_content = _text(packet_response.get("choices", [{}])[0].get("message", {}).get("content"))
        if chapter_packet_response_hook and packet_content:
            chapter_packet_response_hook(packet_content)
        packet_data = _extract_json(packet_content) if packet_content else None
        if not isinstance(packet_data, Mapping):
            packet_audit.update({
                "status": "exploration_rejected_preserved_baseline",
                "errors": ["chapter_packet_response_empty_or_invalid"],
            })
        else:
            parsed_packet_audit, packet_ranks, packet_plan_data, packet_errors = _parse_chapter_packets(
                packet_data, baseline_plan_data=final_data, baseline_path_audit=path_audit,
                baseline_ranks=final_ranked, remaining_tags=enrichment_remaining_tags,
                source_windows=packet_windows,
            )
            packet_audit.update(parsed_packet_audit)
            declared_packet_ids = [
                candidate_id for chapter in _as_mapping_list(packet_plan_data.get("chapters"))
                for candidate_id in _as_int_tuple(chapter.get("candidate_ids"))
            ]
            for candidate_id in declared_packet_ids:
                tag = next((item for item in tags if item.candidate_id == candidate_id), None)
                reject_reason = _quality_final_utterance_reject_reason(tag.text) if tag else "candidate_missing"
                if reject_reason:
                    packet_errors.append(
                        f"chapter_packet_p02_reaudit_known_unplayable:{candidate_id}:{reject_reason}"
                    )
            if not packet_errors:
                packet_validated, packet_path_audit, packet_selected_ids = validate_composition(
                    packet_plan_data, allowed_ranks=packet_ranks,
                )
                if not packet_path_audit.get("passed"):
                    packet_errors.append("chapter_packet_p02_reaudit_purchase_path_invalid")
                    packet_audit["path_errors"] = list(packet_path_audit.get("errors") or ())
                elif not packet_validated.plan_valid:
                    packet_errors.append("chapter_packet_p02_reaudit_plan_invalid")
                    packet_audit["plan_issues"] = list(packet_validated.issues)
                elif _text(packet_audit.get("packet_status")) == "packet_enriched":
                    validated = packet_validated
                    path_audit = packet_path_audit
                    selected_ids = packet_selected_ids
                    final_data = packet_plan_data
                    final_ranked = packet_ranks
                    packet_applied = True
                    packet_audit.update({
                        "status": "packet_enriched_natural_complete",
                        "final_actual_seconds": validated.total_seconds,
                        "final_selected_candidate_ids": list(selected_ids),
                    })
                else:
                    packet_audit.update({
                        "status": "source_depth_limited",
                        "final_actual_seconds": validated.total_seconds,
                        "final_selected_candidate_ids": list(selected_ids),
                    })
            if packet_errors:
                packet_audit.update({
                    "status": "exploration_rejected_preserved_baseline",
                    "errors": list(dict.fromkeys(packet_errors)),
                })

    # P0.3 remains a fallback only when P0.4's M2 decision explicitly says a
    # non-packet single candidate may still be worth considering.  This keeps
    # candidate enrichment available without making it the primary depth path.
    packet_single_candidate_fallback = bool(
        not packet_audit.get("triggered")
        or (
            packet_audit.get("status") == "source_depth_limited"
            and bool(packet_audit.get("single_candidate_enrichment_recommended"))
        )
    )
    # P0.3 has exactly one legal entry point after the packet decision.  The
    # remaining pool excludes known final-utterance hard blocks before it is
    # sent back to M2; other candidates stay available for its own audit.
    enrichment_audit: dict[str, Any] = {
        "stage": "p0_3_narrative_enrichment_single_pass",
        "triggered": False,
        "baseline_actual_seconds": validated.total_seconds,
        "baseline_selected_candidate_ids": list(selected_ids),
        "remaining_safe_executable_candidate_pool_size": len(enrichment_remaining_tags),
        "trigger_conditions": {
            "journey_complete": journey_status == "journey_complete",
            "p0_2_quality_passed": bool(quality_audit is not None and not pre_enrichment_errors and validated.plan_valid),
            "natural_complete_below_target": bool(validated.total_seconds < preferred_low),
            "narrative_depth_45_to_75": narrative_depth is not None,
            "narrative_blueprint_p0_1": _text((narrative_depth or {}).get("blueprint_version")) == "narrative-blueprint-p0.1",
            "remaining_safe_executable": bool(enrichment_remaining_tags),
            "p0_4_single_candidate_fallback": packet_single_candidate_fallback,
        },
    }
    enrichment_applied = False
    enrichment_trigger = all(enrichment_audit["trigger_conditions"].values())
    if not enrichment_trigger:
        enrichment_audit["skip_reason"] = "P0.3 only runs as the P0.4-declared single-candidate fallback after a P0.2 natural completion"
    else:
        enrichment_audit["triggered"] = True
        enrichment_response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_narrative_enrichment_prompt(
                strategy=strategy, director_strategy_contract=dict(director_strategy_contract or {}),
                narrative_depth=narrative_depth or {}, completed_plan_data=final_data,
                completed_path_audit=path_audit, completed_ranks=final_ranked,
                tags=tags, remaining_tags=enrichment_remaining_tags,
                target_duration=target_duration,
            ),
            stage=COMMERCE_NARRATIVE_ENRICHMENT_STAGE, max_tokens=5200,
        )
        enrichment_content = _text(enrichment_response.get("choices", [{}])[0].get("message", {}).get("content"))
        if narrative_enrichment_response_hook and enrichment_content:
            narrative_enrichment_response_hook(enrichment_content)
        enrichment_data = _extract_json(enrichment_content) if enrichment_content else None
        if not isinstance(enrichment_data, Mapping):
            enrichment_audit.update({
                "status": "exploration_rejected_preserved_baseline",
                "errors": ["narrative_enrichment_response_empty_or_invalid"],
            })
        else:
            parsed_audit, enriched_ranks, enriched_data, enrichment_errors = _parse_narrative_enrichment(
                enrichment_data, baseline_plan_data=final_data,
                baseline_path_audit=path_audit, baseline_ranks=final_ranked,
                remaining_tags=enrichment_remaining_tags,
            )
            enrichment_audit.update(parsed_audit)
            # This is the P0.2 whole-transcript re-audit receipt. It receives
            # the final declared order from M2 and only verifies that no known
            # unplayable raw line slipped through; it never supplies a line or
            # repairs an AI decision.
            declared_selected_ids = [
                candidate_id for chapter in _as_mapping_list(enriched_data.get("chapters"))
                for candidate_id in _as_int_tuple(chapter.get("candidate_ids"))
            ]
            for candidate_id in declared_selected_ids:
                tag = next((item for item in tags if item.candidate_id == candidate_id), None)
                reject_reason = _quality_final_utterance_reject_reason(tag.text) if tag else "candidate_missing"
                if reject_reason:
                    enrichment_errors.append(
                        f"narrative_enrichment_p02_reaudit_known_unplayable:{candidate_id}:{reject_reason}"
                    )
            if not enrichment_errors:
                enriched_validated, enriched_path_audit, enriched_selected_ids = validate_composition(
                    enriched_data, allowed_ranks=enriched_ranks,
                )
                if not enriched_path_audit.get("passed"):
                    enrichment_errors.append("narrative_enrichment_p02_reaudit_purchase_path_invalid")
                    enrichment_audit["path_errors"] = list(enriched_path_audit.get("errors") or ())
                elif not enriched_validated.plan_valid:
                    enrichment_errors.append("narrative_enrichment_p02_reaudit_plan_invalid")
                    enrichment_audit["plan_issues"] = list(enriched_validated.issues)
                else:
                    enrichment_status = _text(enrichment_audit.get("enrichment_status"))
                    if enrichment_status == "enriched":
                        validated = enriched_validated
                        path_audit = enriched_path_audit
                        selected_ids = enriched_selected_ids
                        final_data = enriched_data
                        final_ranked = enriched_ranks
                        enrichment_applied = True
                        enrichment_audit.update({
                            "status": "enriched_natural_complete",
                            "final_actual_seconds": validated.total_seconds,
                            "final_selected_candidate_ids": list(selected_ids),
                        })
                    else:
                        # `no_worthwhile_enrichment` is an affirmative M2
                        # decision, not a source failure. The P0.2 baseline is
                        # already complete and deliberately remains untouched.
                        enrichment_audit.update({
                            "status": "source_depth_limited",
                            "final_actual_seconds": validated.total_seconds,
                            "final_selected_candidate_ids": list(selected_ids),
                        })
            if enrichment_errors:
                enrichment_audit.update({
                    "status": "exploration_rejected_preserved_baseline",
                    "errors": list(dict.fromkeys(enrichment_errors)),
                })
    journey_audit["chapter_packet_builder"] = packet_audit
    journey_audit["narrative_enrichment"] = enrichment_audit
    assessment = dict(validated.duration_assessment or {})
    assessment["commerce_strong_clip_ranking"] = {
        "stage": "strong_clip_ranking_purchase_journey_vnext", **rank_audit,
        "ranking": [item.to_dict() for item in ranked],
    }
    assessment["commerce_purchase_cognition_path"] = {
        "stage": "purchase_cognition_composition_stop_rules_vnext", **path_audit,
        "selected_candidate_ids": selected_ids,
        "actual_seconds": validated.total_seconds,
        "natural_ending_allowed": journey_status != "journey_incomplete",
    }
    assessment["commerce_purchase_journey"] = journey_audit
    assessment["commerce_chapter_packet_builder"] = packet_audit
    assessment["commerce_narrative_enrichment"] = enrichment_audit
    if quality_audit is not None:
        assessment["commerce_purchase_journey_quality"] = quality_audit
    all_errors = list(dict.fromkeys(
        list(path_audit["errors"]) + list(journey_errors)
    ))
    if all_errors:
        # Coverage may be complete while the selected spoken material still
        # fails a quality/dependency contract.  That is not a completed
        # journey and must never be surfaced as one merely because Q IDs were
        # enumerated before the final M2 quality pass.
        journey_status = "journey_incomplete"
        old_replan = validated.replan_request
        validated = replace(
            validated, plan_valid=False, status="journey_incomplete",
            issues=tuple(dict.fromkeys(tuple(validated.issues) + tuple(all_errors))),
            replan_request=ReplanRequest(
                reason_codes=tuple(dict.fromkeys(
                    (tuple(old_replan.reason_codes) if old_replan else ()) + tuple(all_errors)
                )),
                detail=tuple(dict.fromkeys(
                    (tuple(old_replan.detail) if old_replan else ()) + tuple(all_errors)
                )),
                affected_chapter_ids=old_replan.affected_chapter_ids if old_replan else (),
            ),
        )
    if quality_audit is not None:
        # These three fields are a receipt for the existing final M2 quality
        # decision.  They do not add a planner, rank a line, or change M3;
        # they make the exact pre-render reasons inspectable by the caller.
        opening_errors = [
            item for item in all_errors
            if "opening" in item or "direct_result" in item
        ]
        order_errors = [
            item for item in all_errors
            if "journey_order" in item or "journey_trust" in item or "journey_styling" in item
        ]
        sentence_errors = [
            item for item in all_errors
            if (
                "known_unplayable_utterance" in item
                or "selected_candidate_not_clean" in item
                or "spoken_completeness" in item
                or "support_not_incremental" in item
            )
        ]
        flow_errors = [item for item in all_errors if "continuity_" in item]
        quality_audit["m3_render_gate"] = {
            "passed": bool(validated.plan_valid and not all_errors),
            "opening_quality": {"passed": not opening_errors, "errors": opening_errors},
            "journey_order": {"passed": not order_errors, "errors": order_errors},
            "sentence_cleanliness": {"passed": not sentence_errors, "errors": sentence_errors},
            "spoken_flow": {"passed": not flow_errors, "errors": flow_errors},
            "chapter_packet_builder": {
                "passed": bool(
                    not packet_audit.get("triggered")
                    or packet_audit.get("status") in {
                        "packet_enriched_natural_complete", "source_depth_limited", "exploration_rejected_preserved_baseline",
                    }
                ),
                "status": _text(packet_audit.get("status")) or "not_triggered",
                "boundary": "P0.4 may only apply M2-declared local Packet candidates; M3 still only materializes final IDs",
            },
            "narrative_enrichment": {
                "passed": bool(
                    not enrichment_audit.get("triggered")
                    or enrichment_audit.get("status") in {"enriched_natural_complete", "source_depth_limited", "exploration_rejected_preserved_baseline"}
                ),
                "status": _text(enrichment_audit.get("status")) or "not_triggered",
                "boundary": "P0.3 may only add M2-declared clean source lines; M3 still only materializes the final IDs",
            },
            "boundary": "M2 validates; M3 may only materialize after this receipt passes",
        }
        journey_audit["m3_render_gate"] = dict(quality_audit["m3_render_gate"])
    # A complete purchase journey may naturally be shorter than a soft target.
    # The previous measured validator correctly measured the seconds but used
    # the misleading ``insufficient_material`` label; only after Completion
    # and Quality both pass can this be a natural ending rather than a source
    # shortage.
    final_issues = list(validated.issues)
    if packet_applied and journey_status == "journey_complete" and validated.plan_valid and not all_errors:
        assessment.update({
            "status": "packet_enriched_natural_complete",
            "reason": (
                f"P0.4 仅加入 M2 从局部 Source Window 恢复的完整 Packet，真实时长自然落为 {validated.total_seconds:.1f}s；"
                "每条新增口播均需提供新的 packet sub-value。"
            ),
            "duration_note": "packet_enriched_without_padding",
            "actual_seconds": validated.total_seconds,
            "preferred_low": preferred_low,
        })
        final_issues = [
            item for item in final_issues
            if not item.startswith("total_")
            and not item.startswith("duration_assessment_corrected_by_measurement")
            and not item.startswith("duration_budget_unfulfilled")
        ]
    elif enrichment_applied and journey_status == "journey_complete" and validated.plan_valid and not all_errors:
        assessment.update({
            "status": "enriched_natural_complete",
            "reason": (
                f"P0.3 仅加入 M2 声明的新购买价值，真实时长自然落为 {validated.total_seconds:.1f}s；"
                "没有为目标时长重复同一购买问题。"
            ),
            "duration_note": "enriched_without_padding",
            "actual_seconds": validated.total_seconds,
            "preferred_low": preferred_low,
        })
        final_issues = [
            item for item in final_issues
            if not item.startswith("total_")
            and not item.startswith("duration_assessment_corrected_by_measurement")
            and not item.startswith("duration_budget_unfulfilled")
        ]
    elif (
        journey_status == "journey_complete"
        and validated.plan_valid
        and validated.total_seconds < preferred_low
        and not all_errors
    ):
        assessment.update({
            "status": "natural_complete_below_target",
            "reason": (
                f"购买旅程已完成，按真实候选自然落为 {validated.total_seconds:.1f}s；"
                f"低于偏好下限 {preferred_low:.1f}s，但没有可加入的新购买价值。"
            ),
            "duration_note": "below_preferred_target",
            "actual_seconds": validated.total_seconds,
            "preferred_low": preferred_low,
        })
        final_issues = [
            item for item in final_issues
            if not item.startswith("total_")
            and not item.startswith("duration_assessment_corrected_by_measurement")
            and not item.startswith("duration_budget_unfulfilled")
        ]
    director_intent = dict(contract.get("director_narrative_contract") or {})
    contract["final_story_brief"] = {
        "authority": "director_narrative_contract",
        "source_lineage": "m1_source_story",
        "narrative_archetype": _text(director_intent.get("narrative_archetype")),
        "core_desire": _text(director_intent.get("core_desire")),
        "opening_promise": _text(director_intent.get("opening_promise")),
        "opening_scope": dict(director_intent.get("opening_scope") or {}),
        "early_journey_scope": dict(director_intent.get("early_journey_scope") or {}),
        "retained_purchase_question_ids": list((quality_audit or {}).get("retained_question_ids") or ()),
        "selected_candidate_ids": list(selected_ids),
        "actual_seconds": validated.total_seconds,
        "chapter_packet_status": _text(packet_audit.get("status")) or "not_triggered",
        "chapter_packet_added_candidate_ids": list(packet_audit.get("added_candidate_ids") or ()),
        "narrative_enrichment_status": _text(enrichment_audit.get("status")) or "not_triggered",
        "narrative_enrichment_added_candidate_ids": list(enrichment_audit.get("added_candidate_ids") or ()),
    }
    assessment["m1_source_story"] = dict(contract.get("m1_source_story") or {})
    assessment["director_narrative_contract"] = director_intent
    assessment["final_story_brief"] = dict(contract["final_story_brief"])
    return ranked, replace(
        validated,
        status=journey_status,
        issues=tuple(final_issues),
        selection_contract=contract,
        duration_assessment=assessment,
    )


def _narrative_mode_default_contract() -> dict[str, Any]:
    """Return the non-archetype fallback without selecting any source Beat."""
    return {
        "narrative_archetype": "purchase_journey",
        "core_desire": "让目标用户理解产品的核心购买价值并自然解除关键顾虑。",
        "opening_promise": "先给出可独立成立的核心购买结果。",
        "opening_scope": {
            "allowed_purchase_question_ids": ["Q1"],
            "allowed_answer_roles": ["result", "proof"],
            "fallback_to_global_opening": False,
            "requires_clean_independent_utterance": True,
        },
        "early_journey_scope": {
            "opening_question_ids": ["Q1", "Q2"],
            "required_question_ids": ["Q1", "Q2"],
            "recommended_question_ids": ["Q3", "Q4", "Q5", "Q6"],
            "optional_question_ids": ["Q7"],
            "preferred_question_order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
        },
        "blueprint": {
            "version": "narrative-mode-default-v1",
            "duration_policy": "soft_target_no_padding",
            "stop_rule": "stop_when_no_new_high_value_journey_step_can_be_supported",
            "chapter_slots": [
                {"slot_id": "q1", "priority": 1, "coverage": "required", "purchase_question_id": "Q1", "answer_roles": ["result", "proof"]},
                {"slot_id": "q2", "priority": 2, "coverage": "required", "purchase_question_id": "Q2", "answer_roles": ["mechanism", "proof"]},
                {"slot_id": "q3", "priority": 3, "coverage": "recommended", "purchase_question_id": "Q3", "answer_roles": ["result", "proof", "risk_remove"]},
                {"slot_id": "q4", "priority": 4, "coverage": "recommended", "purchase_question_id": "Q4", "answer_roles": ["comfort", "proof", "risk_remove"]},
                {"slot_id": "q5", "priority": 5, "coverage": "recommended", "purchase_question_id": "Q5", "answer_roles": ["risk_remove", "proof", "comfort"]},
                {"slot_id": "q6", "priority": 6, "coverage": "recommended", "purchase_question_id": "Q6", "answer_roles": ["styling", "scene", "result", "proof"]},
                {"slot_id": "q7", "priority": 7, "coverage": "optional", "purchase_question_id": "Q7", "answer_roles": ["trust", "proof", "mechanism"]},
            ],
        },
    }


def _narrative_mode_contract(director_strategy_contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize Director Blueprint metadata, never an editorial selection."""
    supplied = dict(director_strategy_contract or {})
    if not supplied.get("blueprint"):
        supplied = _narrative_mode_default_contract() | supplied
    blueprint = dict(supplied.get("blueprint") or {})
    slots: list[dict[str, Any]] = []
    for raw in blueprint.get("chapter_slots") or ():
        if not isinstance(raw, Mapping):
            continue
        question_id = _text(raw.get("purchase_question_id"))
        coverage = _text(raw.get("coverage")).lower()
        roles = tuple(_text(item).lower() for item in raw.get("answer_roles") or () if _text(item))
        if question_id not in PURCHASE_JOURNEY_BY_ID or coverage not in {"required", "recommended", "optional"} or not roles:
            continue
        slots.append({
            "slot_id": _text(raw.get("slot_id")) or question_id.lower(),
            "priority": int(raw.get("priority") or len(slots) + 1),
            "coverage": coverage,
            "purchase_question_id": question_id,
            "answer_roles": list(roles),
        })
    if not slots:
        return _narrative_mode_default_contract()
    slots.sort(key=lambda item: (int(item["priority"]), item["purchase_question_id"]))
    early = dict(supplied.get("early_journey_scope") or {})
    opening = dict(supplied.get("opening_scope") or {})
    allowed_questions = [
        _text(item) for item in opening.get("allowed_purchase_question_ids") or ()
        if _text(item) in PURCHASE_JOURNEY_BY_ID
    ] or [slots[0]["purchase_question_id"]]
    allowed_roles = [
        _text(item).lower() for item in opening.get("allowed_answer_roles") or () if _text(item)
    ] or list(slots[0]["answer_roles"])
    preferred_order = [
        _text(item) for item in early.get("preferred_question_order") or ()
        if _text(item) in {slot["purchase_question_id"] for slot in slots}
    ] or [slot["purchase_question_id"] for slot in slots]
    return {
        "narrative_archetype": _text(supplied.get("narrative_archetype")) or "purchase_journey",
        "core_desire": _text(supplied.get("core_desire")),
        "opening_promise": _text(supplied.get("opening_promise")),
        "opening_scope": {
            "allowed_purchase_question_ids": allowed_questions,
            "allowed_answer_roles": allowed_roles,
            "fallback_to_global_opening": False,
            "requires_clean_independent_utterance": True,
        },
        "early_journey_scope": {
            "required_question_ids": [slot["purchase_question_id"] for slot in slots if slot["coverage"] == "required"],
            "recommended_question_ids": [slot["purchase_question_id"] for slot in slots if slot["coverage"] == "recommended"],
            "optional_question_ids": [slot["purchase_question_id"] for slot in slots if slot["coverage"] == "optional"],
            "preferred_question_order": preferred_order,
        },
        "blueprint": {
            "version": _text(blueprint.get("version")) or "narrative-mode-v1",
            "duration_policy": _text(blueprint.get("duration_policy")) or "soft_target_no_padding",
            "stop_rule": _text(blueprint.get("stop_rule")) or "stop_when_no_new_high_value_journey_step_can_be_supported",
            "chapter_slots": slots,
        },
    }


def _narrative_mode_inventory_summary(beat_candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Expose only aggregate inventory capability before Beat Casting."""
    grouped: dict[str, dict[str, Any]] = {}
    for item in beat_candidates:
        function = _text(item.get("evidence_function")).lower() or "other"
        row = grouped.setdefault(function, {
            "evidence_function": function,
            "beat_count": 0,
            "clean_count": 0,
            "visual_count": 0,
            "high_priority_count": 0,
            "purchase_value_hints": [],
        })
        row["beat_count"] += 1
        if _text(item.get("publishability_status")) == "publishable_clean":
            row["clean_count"] += 1
        else:
            row["visual_count"] += 1
        if _text(item.get("narrative_priority")) == "high":
            row["high_priority_count"] += 1
        hint = _text(item.get("purchase_value")) or _text(item.get("sub_outcome"))
        if hint and hint not in row["purchase_value_hints"]:
            row["purchase_value_hints"].append(hint)
    return [
        {**item, "purchase_value_hints": item["purchase_value_hints"][:6]}
        for _function, item in sorted(grouped.items())
    ]


def build_narrative_mode_journey_prompt(
    *, strategy: Any, narrative_contract: Mapping[str, Any], beat_candidates: Sequence[Mapping[str, Any]],
    target_duration: float,
) -> str:
    """Director pass: define an unscripted desire and buyer journey only."""
    slots = list((narrative_contract.get("blueprint") or {}).get("chapter_slots") or ())
    schema = {
        "core_desire": "", "opening_promise": "",
        "purchase_journey": [{
            "step_id": "J1", "purchase_question_id": "Q1", "purchase_question": "",
            "coverage": "required/recommended/optional", "answer_role_intent": "result",
            "goal": "", "why_now": "",
        }],
        "stop_intent": "", "reason": "",
    }
    return "\n\n".join((
        "你是 LiveClipper 的 AI 商业导演。现在只决定这条视频要让观众相信什么，以及观众要依次经过哪些购买认知；绝对不能选候选、不能输出 beat_id/candidate_id、不能引用/复述任何具体主播原话、不能写脚本或口播句。下一步会由另一项 AI Beat Casting 从完整 P0.5A.3 Actor Pool 选择真实原话。",
        "核心原则：短、快、真实、有证据；微观每 2–8 秒要推进新购买认知，宏观必须围绕一个核心购买欲望。不是卖点清单，也不为了目标时长覆盖所有问题。required 必须优先；recommended 只在仍能让购买说服前进时保留；optional 没有明确增益就主动放弃。",
        "购买路径必须按当前 Director Blueprint 的顺序取一个不跳跃的子序列；第一个问题必须属于 opening_scope。每一项回答一个新的购买问题，或是前一问题必要的 mechanism/proof，不能用同义证明填充。只输出抽象的购买目标，不要暗含某个具体句子。",
        "严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "冻结 M1 来源故事（只作为商品理解来源）：\n" + json.dumps({
            "strategy_id": _text(getattr(strategy, "strategy_id", "")),
            "thesis": _text(getattr(strategy, "thesis", "")),
            "audience_tension": _text(getattr(strategy, "audience_tension", "")),
            "transformation": _text(getattr(strategy, "transformation", "")),
            "product_role": _text(getattr(strategy, "product_role", "")),
        }, ensure_ascii=False, separators=(",", ":")),
        "当前 Director Blueprint（不是候选列表）：\n" + json.dumps(narrative_contract, ensure_ascii=False, separators=(",", ":")),
        f"本次软目标时长：{float(target_duration):.1f}s；不以达标为停止条件。",
        "P0.5A.3 已审计库存能力摘要（不含任何具体句子，不能把它当选片）：\n" + json.dumps(
            _narrative_mode_inventory_summary(beat_candidates), ensure_ascii=False, separators=(",", ":")
        ),
    ))


def _parse_narrative_mode_journey(
    data: Mapping[str, Any], *, narrative_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate the director's route without supplying a story or a Beat."""
    errors: list[str] = []
    slots = list((narrative_contract.get("blueprint") or {}).get("chapter_slots") or ())
    slot_by_question = {str(item["purchase_question_id"]): dict(item) for item in slots}
    order = list((narrative_contract.get("early_journey_scope") or {}).get("preferred_question_order") or ())
    order_index = {question_id: index for index, question_id in enumerate(order)}
    raw_steps = data.get("purchase_journey") or ()
    if isinstance(raw_steps, Mapping):
        raw_steps = (raw_steps,)
    steps: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    previous_order = -1
    for index, raw in enumerate(raw_steps if isinstance(raw_steps, Sequence) else (), start=1):
        if not isinstance(raw, Mapping):
            errors.append(f"journey_step_invalid:{index}")
            continue
        prohibited = {str(key).lower() for key in raw}.intersection({"candidate_id", "candidate_ids", "beat_id", "beat_ids", "text", "spoken_text", "quote"})
        if prohibited:
            errors.append(f"journey_contains_specific_utterance_or_candidate:{index}")
            continue
        question_id = _text(raw.get("purchase_question_id"))
        slot = slot_by_question.get(question_id)
        coverage = _text(raw.get("coverage")).lower()
        role = _text(raw.get("answer_role_intent")).lower()
        if slot is None:
            errors.append(f"journey_question_outside_blueprint:{question_id or index}")
            continue
        if question_id in seen_questions:
            errors.append(f"journey_question_repeated:{question_id}")
            continue
        if coverage != _text(slot.get("coverage")).lower():
            errors.append(f"journey_coverage_mismatch:{question_id}")
        if role not in {_text(value).lower() for value in slot.get("answer_roles") or ()}:
            errors.append(f"journey_answer_role_outside_slot:{question_id}")
        current_order = order_index.get(question_id, len(order) + index)
        if current_order < previous_order:
            errors.append(f"journey_order_breaks_blueprint:{question_id}")
        previous_order = current_order
        if not _text(raw.get("goal")) or not _text(raw.get("why_now")):
            errors.append(f"journey_goal_or_transition_missing:{question_id}")
        seen_questions.add(question_id)
        steps.append({
            "step_id": _text(raw.get("step_id")) or f"J{index}",
            "purchase_question_id": question_id,
            "purchase_question": _text(raw.get("purchase_question")) or _text(PURCHASE_JOURNEY_BY_ID[question_id]["purchase_question"]),
            "coverage": coverage, "answer_role_intent": role,
            "goal": _text(raw.get("goal")), "why_now": _text(raw.get("why_now")),
        })
    required = {str(item["purchase_question_id"]) for item in slots if _text(item.get("coverage")).lower() == "required"}
    missing_required = sorted(required - seen_questions)
    if missing_required:
        errors.append("journey_required_slots_missing:" + ",".join(missing_required))
    opening_scope = dict(narrative_contract.get("opening_scope") or {})
    allowed_opening = {_text(item) for item in opening_scope.get("allowed_purchase_question_ids") or ()}
    if not steps:
        errors.append("journey_missing")
    elif steps[0]["purchase_question_id"] not in allowed_opening:
        errors.append("journey_opening_outside_opening_scope")
    if not _text(data.get("core_desire")):
        errors.append("journey_core_desire_missing")
    if not _text(data.get("opening_promise")):
        errors.append("journey_opening_promise_missing")
    return ({
        "core_desire": _text(data.get("core_desire")),
        "opening_promise": _text(data.get("opening_promise")),
        "purchase_journey": steps,
        "stop_intent": _text(data.get("stop_intent")),
        "reason": _text(data.get("reason")),
    }, list(dict.fromkeys(errors)))


def build_narrative_mode_beat_casting_prompt(
    *, strategy: Any, narrative_contract: Mapping[str, Any], journey: Mapping[str, Any],
    beat_candidates: Sequence[Mapping[str, Any]], target_duration: float,
    approved_opening_packages: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Let the AI cast source Beats into the fixed journey.

    P0.5A.4 may supply a small, AI-approved Hook -> immediate-payoff catalog.
    The catalog is a formal opening boundary, not a programmatic semantic
    selection: the Beat Caster still chooses which approved package, if any,
    serves this Journey.
    """
    rows = [{
        "candidate_id": item.get("candidate_id"), "beat_id": item.get("beat_id"),
        "duration_seconds": item.get("duration_seconds"), "text": item.get("text"),
        "publishability_status": item.get("publishability_status"),
        "visual_dependency": item.get("visual_dependency"),
        "audio_only_eligible": item.get("audio_only_eligible"),
        "hook_eligible": item.get("hook_eligible"),
        "narrative_priority": item.get("narrative_priority"),
        "purchase_value": item.get("purchase_value"), "sub_outcome": item.get("sub_outcome"),
        "evidence_function": item.get("evidence_function"),
    } for item in beat_candidates]
    schema = {
        "casts": [{
            "step_id": "J1", "purchase_question_id": "Q1", "decision": "cast/omit",
            "candidate_ids": [950000001], "answer_role": "result", "supports_question_id": "",
            "purchase_outcome": "", "why_it_advances": "", "transition_from_previous": "",
        }],
        "opening_package": {
            "hook_candidate_id": 950000001, "payoff_candidate_ids": [950000002],
            "promise": "", "payoff_relation": "", "connection_reason": "", "hook_integrity_reason": "",
        },
        "reason": "",
    }
    return "\n\n".join((
        "你现在只做 Beat Casting：把已确定的购买旅程用真实 P0.5 Micro Beat 演出来。每个被选 Beat 都是已完成词级边界、2–8 秒、真实主播原话；你只能选择给定 candidate_id，不能改写文本、不能裁词、不能编造、不能新增购买问题或改变 Journey 顺序。",
        "每个 Journey Step 最多选 2 条：第一条是最强 anchor，第二条只有提供必要且不同角色证据时才允许。不得复用 candidate_id，不得用同一 purchase_question/outcome/role 的重复证明凑时长。required 必须 cast；recommended 可在没有强新推进时 omit；optional 应主动 omit，除非它显著加强当前核心欲望。",
        "Opening 合同：第一步第一条必须等于 opening_package.hook_candidate_id，且只能使用 hook_eligible=true / publishable_clean 的 Beat；publishable_visual 禁止当 Opening，但可作为正文的画面依赖证据。Hook 的商业价值和独立开场资格必须同时成立。第二步的所有 candidate_ids 必须完整、同序地等于 opening_package.payoff_candidate_ids；它们一起构成紧接 Hook 的必要 payoff，不能跳到无关卖点。若下方存在 P0.5A.4 已批准 Opening Package，必须从其中选择一个完整 hook+payoff 配对，严禁自己拼一个看似合理但未通过 Package 审计的开头。",
        "在 answer_role / supports_question_id / purchase_outcome 中写出你对所选真实 Beat 的关系判断。supports_question_id 只能填写一个已经在前面 Journey Step 回答过、且不同于当前 purchase_question_id 的问题；独立回答本题时必须是空字符串。特别是 Q1 永远填写空字符串；Q2 如果是解释 Q1 才填写 \"Q1\"，绝不能填写自己的 \"Q2\"。程序只验证 ID、重复、阶段范围、hook 权限、时长和词级 lineage；哪条候选最强、最顺、最能说服人完全由你决定。",
        "严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "已冻结 Director Core Desire + Purchase Journey（不可改写）：\n" + json.dumps(journey, ensure_ascii=False, separators=(",", ":")),
        "Blueprint 与 Opening Scope：\n" + json.dumps(narrative_contract, ensure_ascii=False, separators=(",", ":")),
        ("P0.5A.4 已批准 Opening Package（只可从中选择一组；由你决定哪一组最适合当前 Journey）：\n" + json.dumps([
            {
                "opening_id": item.get("opening_id"), "hook_candidate_id": item.get("hook_candidate_id"),
                "payoff_candidate_ids": item.get("payoff_candidate_ids"), "quality": item.get("quality"),
                "opening_promise": item.get("opening_promise"), "payoff_relation": item.get("payoff_relation"),
                "why_viewer_keeps_watching": item.get("why_viewer_keeps_watching"),
            } for item in approved_opening_packages
        ], ensure_ascii=False, separators=(",", ":"))) if approved_opening_packages else
        "P0.5A.4 没有可用 Opening Package；不得伪造开场。",
        f"软目标时长 {float(target_duration):.1f}s；停止条件是没有新的高价值购买推进，不是凑时长。",
        "完整 P0.5A.3 clean/visual Actor Pool + P0.5A.4 Hook Overlay（这是唯一选材入口，绝非 Strong Ranking/Top12）：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _parse_narrative_mode_casting(
    data: Mapping[str, Any], *, journey: Mapping[str, Any], beat_candidates: Sequence[Mapping[str, Any]],
    approved_opening_packages: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    errors: list[str] = []
    by_id = {int(item["candidate_id"]): dict(item) for item in beat_candidates if item.get("candidate_id")}
    journey_steps = [dict(item) for item in journey.get("purchase_journey") or () if isinstance(item, Mapping)]
    journey_by_id = {_text(item.get("step_id")): item for item in journey_steps}
    raw_casts = data.get("casts") or ()
    if isinstance(raw_casts, Mapping):
        raw_casts = (raw_casts,)
    casts_by_step: dict[str, Mapping[str, Any]] = {}
    for raw in raw_casts if isinstance(raw_casts, Sequence) else ():
        if not isinstance(raw, Mapping):
            errors.append("beat_casting_row_invalid")
            continue
        step_id = _text(raw.get("step_id"))
        if not step_id or step_id in casts_by_step:
            errors.append(f"beat_casting_step_duplicate_or_missing:{step_id or 'unknown'}")
            continue
        casts_by_step[step_id] = raw
    casts: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for index, step in enumerate(journey_steps, start=1):
        step_id = _text(step.get("step_id"))
        raw = casts_by_step.get(step_id)
        if raw is None:
            errors.append(f"beat_casting_step_missing:{step_id}")
            continue
        question_id = _text(step.get("purchase_question_id"))
        candidate_ids = tuple(_as_int_tuple(raw.get("candidate_ids")))
        decision = _text(raw.get("decision")).lower()
        if _text(raw.get("purchase_question_id")) != question_id:
            errors.append(f"beat_casting_question_mismatch:{step_id}")
        if decision not in {"cast", "omit"}:
            errors.append(f"beat_casting_decision_invalid:{step_id}")
        if decision == "omit":
            if candidate_ids:
                errors.append(f"beat_casting_omit_has_candidates:{step_id}")
            if _text(step.get("coverage")) == "required":
                errors.append(f"beat_casting_required_step_omitted:{step_id}")
            continue
        if not candidate_ids or len(candidate_ids) > 2:
            errors.append(f"beat_casting_candidate_count_invalid:{step_id}")
            continue
        if any(candidate_id not in by_id for candidate_id in candidate_ids):
            errors.append(f"beat_casting_unknown_candidate:{step_id}")
            continue
        if any(candidate_id in used_ids for candidate_id in candidate_ids):
            errors.append(f"beat_casting_candidate_reused:{step_id}")
            continue
        used_ids.update(candidate_ids)
        role = _text(raw.get("answer_role")).lower()
        if role not in {_text(step.get("answer_role_intent")).lower()}:
            errors.append(f"beat_casting_answer_role_mismatch:{step_id}")
        supports = _text(raw.get("supports_question_id"))
        if supports and supports not in [str(item.get("purchase_question_id")) for item in journey_steps[:index - 1]]:
            errors.append(f"beat_casting_support_before_question:{step_id}:{supports}")
        if not _text(raw.get("purchase_outcome")) or not _text(raw.get("why_it_advances")):
            errors.append(f"beat_casting_purchase_relation_missing:{step_id}")
        casts.append({
            "step_id": step_id, "purchase_question_id": question_id,
            "purchase_question": _text(step.get("purchase_question")), "coverage": _text(step.get("coverage")),
            "candidate_ids": list(candidate_ids), "answer_role": role, "supports_question_id": supports,
            "purchase_outcome": _text(raw.get("purchase_outcome")),
            "why_it_advances": _text(raw.get("why_it_advances")),
            "transition_from_previous": _text(raw.get("transition_from_previous")),
        })
    extra = sorted(set(casts_by_step) - set(journey_by_id))
    if extra:
        errors.append("beat_casting_unknown_steps:" + ",".join(extra))
    if len(casts) < 2:
        errors.append("beat_casting_requires_hook_and_payoff")
    opening = dict(data.get("opening_package") or {})
    if casts:
        hook_id = _as_int_tuple(opening.get("hook_candidate_id"))
        if len(hook_id) != 1 or hook_id[0] != casts[0]["candidate_ids"][0]:
            errors.append("beat_casting_opening_hook_mismatch")
        elif not bool(by_id[hook_id[0]].get("hook_eligible")):
            errors.append("beat_casting_opening_hook_ineligible")
    if len(casts) >= 2:
        payoff_ids = _as_int_tuple(opening.get("payoff_candidate_ids"))
        # Casting is the only semantic selection.  The opening-package field
        # is merely a redundant receipt for C2; an AI may name C2's anchor
        # only even when it intentionally casts a second necessary proof.
        # Normalize this receipt to the already-selected, ordered C2 IDs
        # rather than letting duplicated metadata make M3 lose a valid
        # mechanism pair. This never adds or replaces a Beat.
        if not payoff_ids or payoff_ids[0] != casts[1]["candidate_ids"][0]:
            errors.append("beat_casting_opening_payoff_mismatch")
        opening["payoff_candidate_ids_declared"] = list(payoff_ids)
        opening["payoff_candidate_ids"] = list(casts[1]["candidate_ids"])
    approved_pairs = {
        (int(item["hook_candidate_id"]), tuple(_as_int_tuple(item.get("payoff_candidate_ids"))))
        for item in approved_opening_packages
        if item.get("hook_candidate_id") is not None and _as_int_tuple(item.get("payoff_candidate_ids"))
    }
    if approved_pairs and casts:
        selected_hook = _as_int_tuple(opening.get("hook_candidate_id"))
        selected_payoff = tuple(_as_int_tuple(opening.get("payoff_candidate_ids")))
        if len(selected_hook) != 1 or (selected_hook[0], selected_payoff) not in approved_pairs:
            errors.append("p0_5a4_opening_package_not_selected")
    for key in ("promise", "payoff_relation", "connection_reason"):
        if not _text(opening.get(key)):
            errors.append(f"beat_casting_opening_{key}_missing")
    return casts, opening, list(dict.fromkeys(errors))


def _narrative_mode_plan(
    *, strategy: Any, target_duration: float, base_contract: Mapping[str, Any], narrative_contract: Mapping[str, Any],
    journey: Mapping[str, Any], casts: Sequence[Mapping[str, Any]], opening: Mapping[str, Any],
    beat_candidates: Sequence[Mapping[str, Any]],
) -> NarrativePlan:
    by_id = {int(item["candidate_id"]): item for item in beat_candidates if item.get("candidate_id")}
    candidates = tuple(item["planning_candidate"] for item in beat_candidates if item.get("planning_candidate") is not None)
    beats: list[NarrativeBeat] = []
    for index, cast in enumerate(casts, start=1):
        candidate_ids = tuple(_as_int_tuple(cast.get("candidate_ids")))
        duration = round(sum(
            float(by_id[candidate_id]["planning_candidate"].duration) for candidate_id in candidate_ids
        ), 3)
        question_id = _text(cast.get("purchase_question_id"))
        beats.append(NarrativeBeat(
            source_role="p0_5a3_micro_beat",
            narrative_role="hook" if index == 1 else "payoff" if index == 2 else "purchase_journey",
            goal=_text(cast.get("purchase_question")),
            candidate_evidence=candidate_ids,
            required=_text(cast.get("coverage")) == "required",
            target_seconds=duration,
            selection_instruction=_text(cast.get("why_it_advances")),
            chapter_id=f"C{index}",
            selection_origin="AI_beat_casting_from_P0_5A3_actor_pool_with_P0_5A4_opening_package",
            transition_from_previous=_text(cast.get("transition_from_previous")),
            value_dimension=question_id.lower(),
            purchase_value_dimension="same_claim_additional_proof" if _text(cast.get("supports_question_id")) else "new_outcome",
            purchase_value_domain=question_id.lower(),
            purchase_value_outcomes=(_text(cast.get("purchase_outcome")),),
            purchase_value_reason=_text(cast.get("why_it_advances")),
            story_support=_text(cast.get("why_it_advances")),
            commerce_beat_id=question_id,
        ))
    hook_ids = tuple(_as_int_tuple(opening.get("hook_candidate_id")))
    payoff_ids = tuple(_as_int_tuple(opening.get("payoff_candidate_ids")))
    director_contract = dict(narrative_contract) | {
        "authority": "director_narrative_contract",
        "core_desire": _text(journey.get("core_desire")),
        "opening_promise": _text(journey.get("opening_promise")),
    }
    contract = dict(base_contract) | {
        "narrative_mode_p0_5a3_p0_5a4": True,
        "strong_clip_ranking_used_before_journey": False,
        "beat_casting_inventory": "P0_5A3_publishable_clean_and_visual_actor_pool_plus_P0_5A4_hook_overlay",
        "m1_consumption_validation_require_supporting_bridge": False,
        "commerce_lite_purchase_value_progression": False,
        "director_narrative_contract": director_contract,
        "final_story_brief": {
            "authority": "director_narrative_contract",
            "source_lineage": "m1_source_story",
            "narrative_archetype": _text(narrative_contract.get("narrative_archetype")),
            "core_desire": _text(journey.get("core_desire")),
            "opening_promise": _text(journey.get("opening_promise")),
            "purchase_question_ids": [_text(cast.get("purchase_question_id")) for cast in casts],
        },
    }
    raw_plan = NarrativePlan(
        strategy_id=_text(getattr(strategy, "strategy_id", "")),
        thesis=_text(getattr(strategy, "thesis", "")),
        target_duration=float(target_duration),
        beats=tuple(beats), status="journey_complete", recommended_duration=0.0,
        issues=(), removed_beats=(), plan_valid=True,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=OpeningPackage(
            promise=_text(opening.get("promise")), payoff_relation=_text(opening.get("payoff_relation")),
            hook_candidate_ids=hook_ids, payoff_candidate_ids=payoff_ids,
            selection_instruction="AI Beat Casting 已将 P0.5A.3/A.4 实句绑定到 Director Journey。",
            hook_promise=_text(opening.get("promise")),
            payoff_delivery=_text(opening.get("payoff_relation")),
            connection_reason=_text(opening.get("connection_reason")),
            hook_integrity_reason=_text(opening.get("hook_integrity_reason")),
        ),
        selection_contract=contract,
        duration_assessment={"status": "journey_complete", "reason": "P0.5A.3/A.4 Beat Casting 已完成当前 Director Journey。"},
        story_consumption=StoryConsumption(
            hero_strategy_id=_text(getattr(strategy, "strategy_id", "")),
            hero_priority=_text(getattr(strategy, "story_priority", "")),
            hero_consistency_reason="Director Core Desire 继承冻结 M1 商品理解，具体口播由 P0.5A.3/A.4 Beat Casting 执行。",
            supporting_chapter_ids=(), bridge_chapter_ids=(), no_rediscovery=True,
        ),
    )
    validated = validate_narrative_plan(raw_plan, candidates, executable_evidence={
        candidate.candidate_id: {"materializable": True, "origin": "p0_5a3_p0_5a4_boundary_word_exact"}
        for candidate in candidates
    })
    return replace(
        validated,
        status="journey_complete" if validated.plan_valid else "journey_incomplete",
        recommended_duration=validated.total_seconds,
    )


def build_narrative_mode_whole_video_audit_prompt(
    *, journey: Mapping[str, Any], casts: Sequence[Mapping[str, Any]], plan: NarrativePlan,
) -> str:
    by_id = {candidate.candidate_id: candidate for candidate in plan.selected_candidates}
    rows = []
    for index, (cast, beat) in enumerate(zip(casts, plan.beats), start=1):
        rows.append({
            "position": index, "step_id": cast.get("step_id"), "purchase_question_id": cast.get("purchase_question_id"),
            "goal": beat.goal, "answer_role": cast.get("answer_role"),
            "purchase_outcome": cast.get("purchase_outcome"),
            "candidate_ids": list(beat.candidate_ids),
            "spoken_text": [by_id[item].text for item in beat.candidate_ids if item in by_id],
            "seconds": round(sum(by_id[item].duration for item in beat.candidate_ids if item in by_id), 3),
        })
    schema = {
        "status": "pass/needs_recast", "opening_quality": "pass/fail",
        "first_10_second_progression_count": 3, "every_step_new_purchase_value": True,
        "story_focus": "pass/fail", "logic_flow": "pass/fail", "natural_ending": "pass/fail",
        "m3_ready": True, "reason": "", "issues": [],
    }
    return "\n\n".join((
        "你只做 Whole Video Audit，不得增加、删除、重排或替换任何真实 Beat。判断这是不是围绕一个购买欲望、用短而连贯的真实口播逐步说服观众的成片，而不是一串卖点。",
        "必须审：前3秒停留理由与即时兑现；前10秒的新购买认知推进次数；每一步是否确有新购买价值或必要不同证明；是否突然跳题；Supporting 是否压过主故事；是否已经在该结束处自然结束。若任何硬项失败返回 needs_recast 且 m3_ready=false；不要提出程序自动修复方案。",
        "严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "Director Journey：\n" + json.dumps(journey, ensure_ascii=False, separators=(",", ":")),
        "最终已选真实口播顺序（只可审计，不可改动）：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _parse_narrative_mode_whole_video_audit(
    data: Mapping[str, Any], *, total_seconds: float,
) -> dict[str, Any]:
    status = _text(data.get("status")).lower()
    issues = [_text(item) for item in data.get("issues") or () if _text(item)]
    progression = int(data.get("first_10_second_progression_count") or 0)
    required = {
        "opening_quality": _text(data.get("opening_quality")).lower() == "pass",
        "every_step_new_purchase_value": bool(data.get("every_step_new_purchase_value")),
        "story_focus": _text(data.get("story_focus")).lower() == "pass",
        "logic_flow": _text(data.get("logic_flow")).lower() == "pass",
        "natural_ending": _text(data.get("natural_ending")).lower() == "pass",
        "m3_ready": bool(data.get("m3_ready")),
    }
    minimum_progression = 3 if total_seconds >= 10.0 else 2
    if progression < minimum_progression:
        issues.append(f"first_10_second_progression_insufficient:{progression}<{minimum_progression}")
    passed = status == "pass" and all(required.values()) and not issues
    return {
        "status": "pass" if passed else "needs_recast",
        "passed": passed,
        "opening_quality": _text(data.get("opening_quality")),
        "first_10_second_progression_count": progression,
        "minimum_progression_required": minimum_progression,
        "every_step_new_purchase_value": bool(data.get("every_step_new_purchase_value")),
        "story_focus": _text(data.get("story_focus")), "logic_flow": _text(data.get("logic_flow")),
        "natural_ending": _text(data.get("natural_ending")), "m3_ready": bool(data.get("m3_ready")),
        "reason": _text(data.get("reason")), "issues": list(dict.fromkeys(issues)),
        "program_authority": "audit_schema_and_m3_readiness_only_no_semantic_rewrite",
    }


def plan_commerce_lite_narrative_mode_llm(
    *, strategy: Any, beat_candidates: Sequence[Mapping[str, Any]], target_duration: float,
    safe_candidates: Sequence[PlanningCandidate], selection_contract: Mapping[str, Any] | None,
    api_key: str, base_url: str, model: str,
    director_strategy_contract: Mapping[str, Any] | None = None,
    opening_package_provider: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    journey_response_hook: Callable[[str], None] | None = None,
    casting_response_hook: Callable[[str], None] | None = None,
    whole_video_audit_response_hook: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], NarrativePlan]:
    """Narrative Mode: Journey first, then cast from the full P0.5 actor pool.

    This intentionally never invokes Strong Clip Ranking, Top12 truncation,
    Purchase Journey Completion recall, P0.3 enrichment or P0.4 packets.
    The whole clean/visual Beat inventory is the sole source for casting.
    """
    if not beat_candidates or not safe_candidates:
        raise ValueError("Narrative Mode requires a non-empty P0.5 clean/visual Beat execution pool")
    narrative_contract = _narrative_mode_contract(director_strategy_contract)
    journey_response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_narrative_mode_journey_prompt(
            strategy=strategy, narrative_contract=narrative_contract,
            beat_candidates=beat_candidates, target_duration=target_duration,
        ), stage=COMMERCE_NARRATIVE_MODE_JOURNEY_STAGE, max_tokens=4200,
    )
    journey_content = _text(journey_response.get("choices", [{}])[0].get("message", {}).get("content"))
    if journey_response_hook and journey_content:
        journey_response_hook(journey_content)
    journey_data = _extract_json(journey_content) if journey_content else {}
    journey, journey_errors = _parse_narrative_mode_journey(journey_data, narrative_contract=narrative_contract)
    audit: dict[str, Any] = {
        "mode": "P0_5A3_P0_5A4_narrative_mode_journey_then_beat_casting",
        "strong_clip_ranking_used_before_journey": False,
        "strong_clip_top12_used_before_journey": False,
        "full_p0_5a3_clean_visual_actor_pool_size": len(beat_candidates),
        "director_journey": journey,
        "director_journey_errors": journey_errors,
        "beat_casting": None,
        "whole_video_audit": None,
    }
    if journey_errors:
        return audit, NarrativePlan(
            strategy_id=_text(getattr(strategy, "strategy_id", "")), thesis=_text(getattr(strategy, "thesis", "")),
            target_duration=float(target_duration), beats=(), status="journey_incomplete", recommended_duration=0.0,
            issues=tuple(journey_errors), removed_beats=(), plan_valid=False,
            story_brief=CommercialStoryBrief.from_strategy(strategy), selection_contract=dict(selection_contract or {}),
            replan_request=ReplanRequest(tuple(journey_errors), tuple(journey_errors)),
        )
    casting_candidates = tuple(beat_candidates)
    casting_safe_candidates = tuple(safe_candidates)
    approved_opening_packages: tuple[Mapping[str, Any], ...] = ()
    if opening_package_provider is not None:
        supplied = dict(opening_package_provider(journey) or {})
        provider_errors = [
            _text(item) for item in supplied.get("errors") or () if _text(item)
        ]
        supplied_candidates = tuple(item for item in supplied.get("beat_candidates") or () if isinstance(item, Mapping))
        supplied_safe_candidates = tuple(supplied.get("safe_candidates") or ())
        approved_opening_packages = tuple(
            dict(item) for item in supplied.get("approved_opening_packages") or () if isinstance(item, Mapping)
        )
        audit["p0_5a4_opening_package"] = dict(supplied.get("audit") or {})
        if supplied_candidates:
            casting_candidates = supplied_candidates
        if supplied_safe_candidates:
            casting_safe_candidates = supplied_safe_candidates
        if provider_errors or not approved_opening_packages:
            issues = tuple(provider_errors or ("p0_5a4_opening_package_material_limited",))
            audit["beat_casting"] = {"status": "not_run_opening_package_unavailable", "errors": list(issues)}
            audit["whole_video_audit"] = {"status": "not_run_opening_package_unavailable", "issues": list(issues)}
            return audit, NarrativePlan(
                strategy_id=_text(getattr(strategy, "strategy_id", "")), thesis=_text(getattr(strategy, "thesis", "")),
                target_duration=float(target_duration), beats=(), status="journey_incomplete", recommended_duration=0.0,
                issues=issues, removed_beats=(), plan_valid=False,
                story_brief=CommercialStoryBrief.from_strategy(strategy), selection_contract=dict(selection_contract or {}),
                replan_request=ReplanRequest(issues, issues),
            )
    casting_response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_narrative_mode_beat_casting_prompt(
            strategy=strategy, narrative_contract=narrative_contract, journey=journey,
            beat_candidates=casting_candidates, target_duration=target_duration,
            approved_opening_packages=approved_opening_packages,
        ), stage=COMMERCE_NARRATIVE_MODE_BEAT_CASTING_STAGE, max_tokens=5200,
    )
    casting_content = _text(casting_response.get("choices", [{}])[0].get("message", {}).get("content"))
    if casting_response_hook and casting_content:
        casting_response_hook(casting_content)
    casting_data = _extract_json(casting_content) if casting_content else {}
    casts, opening, casting_errors = _parse_narrative_mode_casting(
        casting_data, journey=journey, beat_candidates=casting_candidates,
        approved_opening_packages=approved_opening_packages,
    )
    audit["beat_casting"] = {"casts": casts, "opening_package": opening, "errors": casting_errors}
    if casting_errors:
        audit["whole_video_audit"] = {
            "status": "not_run_beat_casting_contract_invalid",
            "issues": list(casting_errors),
        }
        return audit, NarrativePlan(
            strategy_id=_text(getattr(strategy, "strategy_id", "")), thesis=_text(getattr(strategy, "thesis", "")),
            target_duration=float(target_duration), beats=(), status="journey_incomplete", recommended_duration=0.0,
            issues=tuple(casting_errors), removed_beats=(), plan_valid=False,
            story_brief=CommercialStoryBrief.from_strategy(strategy), selection_contract=dict(selection_contract or {}),
            replan_request=ReplanRequest(tuple(casting_errors), tuple(casting_errors)),
        )
    plan = _narrative_mode_plan(
        strategy=strategy, target_duration=target_duration, base_contract=selection_contract or {},
        narrative_contract=narrative_contract, journey=journey, casts=casts, opening=opening,
        beat_candidates=casting_candidates,
    )
    selected_opening_package = next((
        dict(item) for item in approved_opening_packages
        if int(item.get("hook_candidate_id") or 0) == int(opening.get("hook_candidate_id") or 0)
        and tuple(_as_int_tuple(item.get("payoff_candidate_ids")))
        == tuple(_as_int_tuple(opening.get("payoff_candidate_ids")))
    ), None)
    if selected_opening_package is not None:
        plan = replace(plan, selection_contract=dict(plan.selection_contract or {}) | {
            "p0_5a4_opening_package": {
                "authority": "AI_P0_5A4_opening_package_then_AI_beat_casting",
                "opening_id": _text(selected_opening_package.get("opening_id")),
                "hook_candidate_id": int(selected_opening_package.get("hook_candidate_id") or 0),
                "payoff_candidate_ids": list(_as_int_tuple(selected_opening_package.get("payoff_candidate_ids"))),
                "quality": _text(selected_opening_package.get("quality")),
            },
        })
        if isinstance(audit.get("p0_5a4_opening_package"), Mapping):
            audit["p0_5a4_opening_package"] = dict(audit["p0_5a4_opening_package"]) | {
                "selected_opening_id": _text(selected_opening_package.get("opening_id")),
            }
    if not plan.plan_valid:
        audit["whole_video_audit"] = {"status": "not_run_plan_contract_invalid", "issues": list(plan.issues)}
        return audit, plan
    whole_response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_narrative_mode_whole_video_audit_prompt(journey=journey, casts=casts, plan=plan),
        stage=COMMERCE_NARRATIVE_MODE_WHOLE_VIDEO_AUDIT_STAGE, max_tokens=2600,
    )
    whole_content = _text(whole_response.get("choices", [{}])[0].get("message", {}).get("content"))
    if whole_video_audit_response_hook and whole_content:
        whole_video_audit_response_hook(whole_content)
    whole_data = _extract_json(whole_content) if whole_content else {}
    whole_audit = _parse_narrative_mode_whole_video_audit(whole_data, total_seconds=plan.total_seconds)
    audit["whole_video_audit"] = whole_audit
    assessment = dict(plan.duration_assessment or {}) | {
        "status": "journey_complete" if whole_audit["passed"] else "journey_incomplete",
        "reason": "Whole Video Audit 通过，允许 M3 忠实物化。" if whole_audit["passed"] else "Whole Video Audit 要求重新由 AI Beat Casting 处理；程序不自动换句。",
        "narrative_mode_whole_video_audit": whole_audit,
        "m3_render_gate": {
            "passed": bool(whole_audit["passed"]),
            "reason": "whole_video_audit_passed" if whole_audit["passed"] else "whole_video_audit_needs_recast",
        },
    }
    return audit, replace(
        plan,
        status="journey_complete" if whole_audit["passed"] else "journey_incomplete",
        plan_valid=bool(plan.plan_valid and whole_audit["passed"]),
        issues=tuple(list(plan.issues) + ([] if whole_audit["passed"] else ["whole_video_audit_needs_recast"])),
        duration_assessment=assessment,
    )
