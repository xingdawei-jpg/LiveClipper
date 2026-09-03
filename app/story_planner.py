# -*- coding: utf-8 -*-
"""M2 Commercial Story Planner.

M1 discovers a commercial story and its usable assets.  This module does not
turn those assets into a new candidate gate.  It gives a director the complete
hard-safe pool, asks it to plan chapters around the story brief, and validates
only deterministic facts afterwards.

This is deliberately a prototype boundary: it is not imported by the live
preview or render paths yet.  A later integration must construct
``PlanningCandidate`` objects from the frozen candidate contract / ledger.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any, Callable, Mapping, Sequence

from ai_model_config import ai_chat_completions_url
from ai_cost_ledger import record_ai_call
from commercial_analyzer import EvidenceItem, Strategy
from ssl_context import create_ssl_context


ASSET_TIERS = ("core", "supporting", "bridge")
HOOK_PREFERRED_MIN_SECONDS = 2.5
HOOK_PREFERRED_MAX_SECONDS = 5.0
# The opening needs to be compact, but a complete promise occasionally spans
# two frozen word-boundary candidates.  Eight seconds is the outer tolerance;
# anything above it must be replanned, while 5-8 seconds still needs the
# director's explicit integrity explanation and remains human-reviewable.
HOOK_ACCEPTABLE_MAX_SECONDS = 8.0
OPENING_UNIT_PREFERRED_MAX_SECONDS = 10.0
# Opening duration is a quality signal, not an execution veto.  The Lite
# contract emits a warning after 12 seconds and requests human review after
# 20 seconds; both leave the plan materializable if its evidence is legal.
OPENING_UNIT_WARNING_SECONDS = 12.0
OPENING_UNIT_HUMAN_REVIEW_SECONDS = 20.0
PLAN_STATUS_OK = "ok"
PLAN_STATUS_ACCEPTABLE_SHORT = "acceptable_short"
PLAN_STATUS_ACCEPTABLE_LONG = "acceptable_long"
PLAN_STATUS_INSUFFICIENT_MATERIAL = "insufficient_material"

DURATION_STATUS_FEASIBLE = "feasible"
DURATION_STATUS_INSUFFICIENT_FOR_TARGET = "insufficient_for_target"
DURATION_STATUS_UNREPORTED = "unreported"

DURATION_DEPTH_CORE_DENSE = "core_dense"
DURATION_DEPTH_STORY_WITH_SUPPORT = "story_with_support"
DURATION_DEPTH_CHAPTERED_STORY = "chaptered_story"
DURATION_DEPTH_EXPANDED_STORY = "expanded_story"

DEPTH_EXPANSION_NOT_APPLICABLE = "not_applicable"
DEPTH_EXPANSION_EXPANDED = "expanded"
DEPTH_EXPANSION_INSUFFICIENT_DISTINCT_VALUE = "insufficient_distinct_value"
EXPANSION_SCOUT_FOUND = "found"
EXPANSION_SCOUT_INSUFFICIENT = "insufficient_distinct_value"
EXPANSION_SCOUT_UNREPORTED = "unreported"
DEPTH_NEW_VALUE_DIMENSIONS = frozenset((
    "new_audience",
    "new_scene",
    "new_comfort",
    "new_styling_result",
    "new_objection",
    "new_trust",
    "new_usage_cycle",
    "new_emotional_value",
))
DEPTH_BASE_VALUE_DIMENSIONS = frozenset((
    "core_promise",
    "mechanism",
    "proof",
    "core_result",
    "same_claim_additional_proof",
))


def _as_int_tuple(raw: Any) -> tuple[int, ...]:
    if isinstance(raw, (int, float, str)):
        raw = (raw,)
    result: list[int] = []
    for value in raw or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return tuple(result)


def _as_text_tuple(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = (raw,)
    result: list[str] = []
    for value in raw or ():
        text = str(value or "").strip().lower()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _as_identifier_tuple(raw: Any) -> tuple[str, ...]:
    """Keep chapter IDs case-preserving; they are identifiers, not keywords."""
    if isinstance(raw, str):
        raw = (raw,)
    result: list[str] = []
    for value in raw or ():
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _number(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


@dataclass(frozen=True)
class PlanningCandidate:
    """One immutable, hard-safe candidate available to the M2 director.

    ``origin_subtitle_ids`` preserves the only bridge M1 needs: it lets an M1
    asset reference a frozen candidate without rewriting text or timestamps.
    The production bridge will populate it from CandidateLedger lineage.
    """

    candidate_id: int
    source_id: str
    start: float
    end: float
    text: str
    origin_subtitle_ids: tuple[int, ...] = ()
    hook_eligible: bool = True
    role_permissions: tuple[str, ...] = ("hook", "product")
    subject_relation: str = "main"
    story_block_id: str = ""
    continuity_group_id: str = ""
    asset_tiers: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end - self.start), 3)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PlanningCandidate":
        candidate_id = int(raw.get("candidate_id") or raw.get("id") or 0)
        start = _number(raw.get("start"))
        end = max(start, _number(raw.get("end"), start))
        roles = raw.get("role_permissions") or raw.get("permissions") or ()
        if isinstance(roles, str):
            roles = (roles,)
        return cls(
            candidate_id=candidate_id,
            source_id=str(raw.get("source_id") or raw.get("source") or "SINGLE").strip() or "SINGLE",
            start=start,
            end=end,
            text=str(raw.get("text") or "").strip(),
            origin_subtitle_ids=_as_int_tuple(
                raw.get("origin_subtitle_ids") or raw.get("subtitle_ids") or raw.get("origin_ids")
            ),
            hook_eligible=bool(raw.get("hook_eligible", True)),
            role_permissions=_as_text_tuple(roles) or ("hook", "product"),
            subject_relation=str(raw.get("subject_relation") or "main").strip() or "main",
            story_block_id=str(raw.get("story_block_id") or "").strip(),
            continuity_group_id=str(raw.get("continuity_group_id") or "").strip(),
            asset_tiers=tuple(tier for tier in _as_text_tuple(raw.get("asset_tiers")) if tier in ASSET_TIERS),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": self.duration,
            "text": self.text,
            "origin_subtitle_ids": list(self.origin_subtitle_ids),
            "hook_eligible": self.hook_eligible,
            "role_permissions": list(self.role_permissions),
            "subject_relation": self.subject_relation,
            "story_block_id": self.story_block_id,
            "continuity_group_id": self.continuity_group_id,
            "asset_tiers": list(self.asset_tiers),
        }


@dataclass(frozen=True)
class CommercialStoryBrief:
    """M1's story-level output, passed through without reducing it to thesis."""

    strategy_id: str
    thesis: str
    story_premise: str
    audience_tension: str
    story_trigger: str
    transformation: str
    product_role: str
    core_commercial_idea: str
    payoff: str
    story_priority: str
    supporting_arcs: tuple[str, ...]
    content_dependencies: tuple[str, ...]
    core_assets: tuple[EvidenceItem, ...]
    supporting_assets: tuple[EvidenceItem, ...]
    bridge_assets: tuple[EvidenceItem, ...]

    @classmethod
    def from_strategy(cls, strategy: Strategy) -> "CommercialStoryBrief":
        core = strategy.core_evidence_pool or strategy.evidence
        return cls(
            strategy_id=strategy.strategy_id,
            thesis=strategy.thesis,
            story_premise=strategy.story_premise,
            audience_tension=strategy.audience_tension,
            story_trigger=strategy.story_trigger,
            transformation=strategy.transformation,
            product_role=strategy.product_role,
            core_commercial_idea=strategy.core_commercial_idea or strategy.thesis,
            payoff=strategy.payoff,
            story_priority=strategy.story_priority,
            supporting_arcs=tuple(strategy.supporting_arcs),
            content_dependencies=tuple(strategy.content_dependencies),
            core_assets=tuple(core),
            supporting_assets=tuple(strategy.supporting_evidence_pool),
            bridge_assets=tuple(strategy.bridge_candidates),
        )

    def payload(self) -> dict[str, Any]:
        def evidence_payload(items: Sequence[EvidenceItem]) -> list[dict[str, Any]]:
            return [item.to_dict() for item in items]

        return {
            "strategy_id": self.strategy_id,
            "thesis": self.thesis,
            "story_premise": self.story_premise,
            "audience_tension": self.audience_tension,
            "story_trigger": self.story_trigger,
            "transformation": self.transformation,
            "product_role": self.product_role,
            "core_commercial_idea": self.core_commercial_idea,
            "payoff": self.payoff,
            "story_priority": self.story_priority,
            "supporting_arcs": list(self.supporting_arcs),
            "content_dependencies": list(self.content_dependencies),
            "core_assets": evidence_payload(self.core_assets),
            "supporting_assets": evidence_payload(self.supporting_assets),
            "bridge_assets": evidence_payload(self.bridge_assets),
        }


@dataclass(frozen=True)
class ExecutableEvidence:
    """One M1 evidence reference with its downstream materialization fact.

    This is an M2-only view.  It deliberately does not mutate the M1 brief,
    the Commercial Asset Ledger, or the hard-safe candidate pool.  A false
    value means the commercial evidence remains visible to the director, but
    cannot be submitted to M3 in this run because its verified word boundary
    cannot be materialized.
    """

    candidate_id: int
    commercial_role: str
    story_tier: str
    materializable: bool
    materialization_issue: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "commercial_role": self.commercial_role,
            "story_tier": self.story_tier,
            "materializable": self.materializable,
            "materialization_issue": self.materialization_issue,
        }


def build_executable_evidence_view(
    brief: CommercialStoryBrief,
    safe_candidates: Sequence[PlanningCandidate],
    materialization_facts: Mapping[int, Mapping[str, Any]] | None = None,
) -> tuple[ExecutableEvidence, ...]:
    """Expose M3 materialization facts to M2 without filtering M1 evidence.

    ``safe_candidates`` is still the full *executable* hard-safe pool for the
    current run.  The optional facts retain why an M1 asset is absent from that
    pool, so the model sees the commercial evidence but may not select it.
    """

    executable_ids = {candidate.candidate_id for candidate in safe_candidates}
    facts = {int(candidate_id): dict(value or {}) for candidate_id, value in (materialization_facts or {}).items()}
    candidate_ids_by_subtitle: dict[int, list[int]] = {}
    for candidate in safe_candidates:
        for subtitle_id in candidate.origin_subtitle_ids:
            candidate_ids_by_subtitle.setdefault(int(subtitle_id), []).append(candidate.candidate_id)
    for candidate_id, fact in facts.items():
        for subtitle_id in fact.get("origin_subtitle_ids") or ():
            candidate_ids_by_subtitle.setdefault(int(subtitle_id), []).append(candidate_id)

    result: list[ExecutableEvidence] = []
    seen: set[tuple[int, str, str]] = set()
    for tier, assets in (
        ("core", brief.core_assets),
        ("supporting", brief.supporting_assets),
        ("bridge", brief.bridge_assets),
    ):
        for asset in assets:
            for subtitle_id in asset.subtitle_ids:
                # M1 evidence is subtitle lineage; M2/M3 submit frozen
                # candidate IDs.  Resolve only through that recorded lineage.
                # A missing mapping stays visible as an explicitly unusable
                # pseudo-reference instead of being semantically guessed.
                linked_ids = candidate_ids_by_subtitle.get(int(subtitle_id), ())
                references = linked_ids or (int(subtitle_id),)
                for candidate_id in references:
                    key = (int(candidate_id), str(asset.role or "unknown"), tier)
                    if key in seen:
                        continue
                    seen.add(key)
                    fact = facts.get(int(candidate_id), {})
                    materializable = bool(fact.get("materializable", int(candidate_id) in executable_ids))
                    issue = str(fact.get("materialization_issue") or fact.get("detail") or "").strip()
                    if not materializable and not issue:
                        issue = "not_present_in_executable_candidate_pool"
                    result.append(ExecutableEvidence(
                        candidate_id=int(candidate_id),
                        commercial_role=str(asset.role or "unknown"),
                        story_tier=tier,
                        materializable=materializable,
                        materialization_issue=issue,
                    ))
    return tuple(result)


def bind_story_assets(
    brief: CommercialStoryBrief,
    safe_candidates: Sequence[PlanningCandidate],
) -> tuple[PlanningCandidate, ...]:
    """Annotate all safe candidates with M1 asset tiers; never filter them.

    One candidate may serve multiple roles.  Tier annotations are preferences
    for the director, not an allow-list.  Candidates without M1 evidence remain
    ``safe_reserve`` in prompt presentation and stay selectable.
    """

    subtitle_tiers: dict[int, set[str]] = {}
    for tier, assets in (
        ("core", brief.core_assets),
        ("supporting", brief.supporting_assets),
        ("bridge", brief.bridge_assets),
    ):
        for asset in assets:
            for subtitle_id in asset.subtitle_ids:
                subtitle_tiers.setdefault(int(subtitle_id), set()).add(tier)

    result: list[PlanningCandidate] = []
    for candidate in safe_candidates:
        tiers = set(candidate.asset_tiers)
        for subtitle_id in candidate.origin_subtitle_ids:
            tiers.update(subtitle_tiers.get(subtitle_id, set()))
        ordered = tuple(tier for tier in ASSET_TIERS if tier in tiers)
        # Hook is a role permission, not a text rewrite.  A complete 9-second
        # candidate may remain valuable in the body, but cannot be submitted
        # as a <=8-second opening expression without a real frozen boundary.
        result.append(replace(
            candidate,
            asset_tiers=ordered,
            hook_eligible=(candidate.hook_eligible and candidate.duration <= HOOK_ACCEPTABLE_MAX_SECONDS),
        ))
    return tuple(result)


_UNAPPROVED_THEME_MARKERS = (
    "价格", "折扣", "优惠", "原价", "现价", "库存", "限量", "预售", "下单", "拍链接", "小黄车",
)


def audit_story_consumption(
    plan: "NarrativePlan",
    strategy: Strategy,
    safe_candidates: Sequence[PlanningCandidate],
) -> dict[str, Any]:
    """Audit that an M2 plan executes its M1 brief instead of re-discovering.

    This does not filter candidates or repair the plan.  It only checks the
    explicit consumption declaration and deterministic tier/ID relationships;
    the full hard-safe reserve remains available to the director.
    """
    brief = CommercialStoryBrief.from_strategy(strategy)
    annotated = bind_story_assets(brief, tuple(safe_candidates))
    candidate_by_id = {item.candidate_id: item for item in annotated}
    beat_by_id = {beat.chapter_id: beat for beat in plan.beats if beat.chapter_id}
    selected_ids = [
        candidate_id
        for beat in plan.beats
        for candidate_id in beat.candidate_ids
        if candidate_id in candidate_by_id
    ]
    selected = tuple(candidate_by_id[candidate_id] for candidate_id in dict.fromkeys(selected_ids))
    selected_tiers = {
        tier: sorted({candidate.candidate_id for candidate in selected if tier in candidate.asset_tiers})
        for tier in ASSET_TIERS
    }
    chapter_tiers = {
        tier: sorted({
            beat.chapter_id for beat in plan.beats
            if any(tier in candidate_by_id.get(candidate_id, PlanningCandidate(0, "", 0, 0, "")).asset_tiers for candidate_id in beat.candidate_ids)
        })
        for tier in ("supporting", "bridge")
    }
    consumption = plan.story_consumption
    selection_contract = dict(plan.selection_contract or {})
    final_story_brief = selection_contract.get("final_story_brief")
    director_narrative_authority = bool(
        isinstance(final_story_brief, Mapping)
        and str(final_story_brief.get("authority") or "").strip() == "director_narrative_contract"
    )
    issues: list[str] = []
    declaration_warnings: list[str] = []
    if consumption is None:
        issues.append("story_consumption_missing")
    else:
        if consumption.hero_strategy_id != strategy.strategy_id:
            issues.append("hero_strategy_mismatch")
        if consumption.hero_priority != strategy.story_priority:
            issues.append("hero_priority_mismatch")
        if not consumption.hero_consistency_reason:
            issues.append("hero_consistency_reason_missing")
        if not consumption.no_rediscovery:
            issues.append("no_rediscovery_not_attested")

        for chapter_id in consumption.supporting_chapter_ids:
            beat = beat_by_id.get(chapter_id)
            if beat is None:
                declaration_warnings.append(f"supporting_chapter_unknown:{chapter_id}")
            elif not beat.story_support:
                issues.append(f"supporting_chapter_reason_missing:{chapter_id}")
            elif not any("supporting" in candidate_by_id.get(candidate_id, PlanningCandidate(0, "", 0, 0, "")).asset_tiers for candidate_id in beat.candidate_ids):
                declaration_warnings.append(f"supporting_chapter_tier_mismatch:{chapter_id}")
        for chapter_id in consumption.bridge_chapter_ids:
            beat = beat_by_id.get(chapter_id)
            if beat is None:
                declaration_warnings.append(f"bridge_chapter_unknown:{chapter_id}")
            elif not beat.story_support:
                issues.append(f"bridge_chapter_reason_missing:{chapter_id}")
            elif not any("bridge" in candidate_by_id.get(candidate_id, PlanningCandidate(0, "", 0, 0, "")).asset_tiers for candidate_id in beat.candidate_ids):
                declaration_warnings.append(f"bridge_chapter_tier_mismatch:{chapter_id}")

    # A bridge/supporting asset may be commercially valid in M1 but unavailable
    # to this M3 run.  Require consumption only when an executable candidate of
    # that tier is actually present; M1's evidence itself remains untouched.
    has_executable_supporting = bool(selected_tiers["supporting"] or any(
        "supporting" in candidate.asset_tiers for candidate in candidate_by_id.values()
    ))
    has_executable_bridge = bool(selected_tiers["bridge"] or any(
        "bridge" in candidate.asset_tiers for candidate in candidate_by_id.values()
    ))
    if not director_narrative_authority:
        if has_executable_supporting and not selected_tiers["supporting"]:
            issues.append("supporting_asset_not_used")
        if has_executable_bridge and not selected_tiers["bridge"]:
            issues.append("bridge_not_consumed")
        if has_executable_supporting and consumption and not consumption.supporting_chapter_ids:
            issues.append("supporting_chapter_not_declared")
        if has_executable_bridge and consumption and not consumption.bridge_chapter_ids:
            issues.append("bridge_not_consumed")
    else:
        declaration_warnings.append(
            "m1_tier_consumption_advisory:final_story_brief_authority=director_narrative_contract"
        )
    if consumption is not None and not director_narrative_authority:
        declared_bridge_ids = sorted(set(consumption.bridge_candidate_ids))
        actual_bridge_ids = selected_tiers["bridge"]
        if declared_bridge_ids != actual_bridge_ids:
            issues.append(
                "bridge_not_consumed:declared="
                f"{declared_bridge_ids},actual={actual_bridge_ids}"
            )
    for tier in ("supporting", "bridge"):
        for chapter_id in chapter_tiers[tier]:
            if not beat_by_id[chapter_id].story_support:
                issues.append(f"{tier}_actual_chapter_reason_missing:{chapter_id}")
    if any(not beat.story_support for beat in plan.beats):
        issues.append("chapter_story_support_missing")

    if director_narrative_authority:
        # The Director contract is the final story authority.  Its internal
        # goals/reasons are audit metadata, not spoken material; inspect only
        # the actual selected source utterances for forbidden commerce themes.
        # The frozen M1 path below deliberately preserves its stricter legacy
        # metadata audit.
        plan_text = " ".join(candidate.text for candidate in selected)
    else:
        plan_text = " ".join((
            plan.opening_package.promise if plan.opening_package else "",
            plan.opening_package.payoff_relation if plan.opening_package else "",
            *(f"{beat.goal} {beat.selection_instruction} {beat.transition_from_previous} {beat.story_support}" for beat in plan.beats),
            consumption.hero_consistency_reason if consumption else "",
        ))
    theme_hits = sorted({marker for marker in _UNAPPROVED_THEME_MARKERS if marker in plan_text})
    if theme_hits:
        issues.append("unapproved_theme_markers=" + ",".join(theme_hits))
    return {
        "passed": not issues,
        "issues": issues,
        "declaration_warnings": declaration_warnings,
        "hero_strategy_id": strategy.strategy_id,
        "hero_priority": strategy.story_priority,
        "intent_authority": (
            "director_narrative_contract" if director_narrative_authority else "m1_source_story"
        ),
        "m1_source_story": (
            dict(selection_contract.get("m1_source_story") or {}) if selection_contract else brief.payload()
        ),
        "director_narrative_contract": (
            dict(selection_contract.get("director_narrative_contract") or {}) if selection_contract else None
        ),
        "final_story_brief": dict(final_story_brief) if isinstance(final_story_brief, Mapping) else None,
        "selected_asset_tiers": selected_tiers,
        "actual_tier_chapter_ids": chapter_tiers,
        "executable_tier_available": {
            "supporting": has_executable_supporting,
            "bridge": has_executable_bridge,
        },
        "story_consumption": consumption.to_dict() if consumption else None,
        "unapproved_theme_markers": theme_hits,
    }


@dataclass(frozen=True)
class OpeningPackage:
    """A promise + immediate payoff relationship, not two arbitrary chapters."""

    promise: str
    payoff_relation: str
    hook_candidate_ids: tuple[int, ...]
    payoff_candidate_ids: tuple[int, ...]
    selection_instruction: str = ""
    hook_promise: str = ""
    payoff_delivery: str = ""
    connection_reason: str = ""
    hook_integrity_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "promise": self.promise,
            "payoff_relation": self.payoff_relation,
            "hook_candidate_ids": list(self.hook_candidate_ids),
            "payoff_candidate_ids": list(self.payoff_candidate_ids),
            "selection_instruction": self.selection_instruction,
            # The legacy keys remain for existing source-only reports.  The
            # explicit contract keys are what new M2 responses must provide.
            "hook_promise": self.hook_promise or self.promise,
            "payoff_delivery": self.payoff_delivery or self.payoff_relation,
            "connection_reason": self.connection_reason or self.payoff_relation,
            "hook_integrity_reason": self.hook_integrity_reason,
        }


@dataclass(frozen=True)
class NarrativeBeat:
    """A chapter with actual candidate IDs, never rewritten subtitle text."""

    source_role: str
    narrative_role: str
    goal: str
    candidate_evidence: tuple[int, ...]
    required: bool
    target_seconds: float
    selection_instruction: str
    chapter_id: str = ""
    asset_tier: str = ""
    selection_origin: str = ""
    transition_from_previous: str = ""
    value_dimension: str = ""
    # Lite uses this as a declaration of whether this beat introduces a new
    # purchase outcome or is additional proof for an already-established one.
    # It is deliberately separate from the actual domain/outcomes below.
    purchase_value_dimension: str = ""
    # ``purchase_value_domain`` is the broad buyer-result domain (for example
    # ``body_appearance``); outcomes carry the real decision granularity such
    # as ``shoulder_narrowing`` or ``hip_coverage``.  The validator must never
    # reject distinct outcomes merely because their broad domain matches.
    purchase_value_domain: str = ""
    purchase_value_outcomes: tuple[str, ...] = ()
    purchase_value_reason: str = ""
    story_support: str = ""
    # M2.5's purchasing-cognition contract.  This is a semantic beat label,
    # not a candidate filter, score, time range, or new ordering authority.
    commerce_beat_id: str = ""

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return self.candidate_evidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "source_role": self.source_role,
            "narrative_role": self.narrative_role,
            "goal": self.goal,
            "candidate_ids": list(self.candidate_evidence),
            "candidate_evidence": list(self.candidate_evidence),  # legacy reader compatibility
            "required": self.required,
            "target_seconds": self.target_seconds,
            "selection_instruction": self.selection_instruction,
            "asset_tier": self.asset_tier,
            "selection_origin": self.selection_origin,
            "transition_from_previous": self.transition_from_previous,
            "value_dimension": self.value_dimension,
            "purchase_value_dimension": self.purchase_value_dimension,
            "purchase_value_domain": self.purchase_value_domain,
            "purchase_value_outcomes": list(self.purchase_value_outcomes),
            "purchase_value_reason": self.purchase_value_reason,
            "story_support": self.story_support,
            "commerce_beat_id": self.commerce_beat_id,
        }


@dataclass(frozen=True)
class StoryConsumption:
    """M2's auditable declaration that it is executing one M1 story."""

    hero_strategy_id: str
    hero_priority: str
    hero_consistency_reason: str
    supporting_chapter_ids: tuple[str, ...]
    bridge_chapter_ids: tuple[str, ...]
    no_rediscovery: bool
    supporting_candidate_ids: tuple[int, ...] = ()
    bridge_candidate_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hero_strategy_id": self.hero_strategy_id,
            "hero_priority": self.hero_priority,
            "hero_consistency_reason": self.hero_consistency_reason,
            "supporting_chapter_ids": list(self.supporting_chapter_ids),
            "bridge_chapter_ids": list(self.bridge_chapter_ids),
            "no_rediscovery": self.no_rediscovery,
            "supporting_candidate_ids": list(self.supporting_candidate_ids),
            "bridge_candidate_ids": list(self.bridge_candidate_ids),
        }


@dataclass(frozen=True)
class RemovedBeat:
    """Legacy audit shape retained, always empty in M2-B.

    The new planner must return a replan request rather than silently removing
    an optional chapter, so callers can keep reading this field safely while
    observing that it never becomes a local story rewrite.
    """

    role: str
    reason: str
    subtitle_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "reason": self.reason, "subtitle_ids": list(self.subtitle_ids)}


@dataclass(frozen=True)
class ReplanRequest:
    reason_codes: tuple[str, ...]
    detail: tuple[str, ...]
    affected_chapter_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_codes": list(self.reason_codes),
            "detail": list(self.detail),
            "affected_chapter_ids": list(self.affected_chapter_ids),
        }


@dataclass(frozen=True)
class DurationPlan:
    """The director's story-depth budget, separate from measured clip length.

    M2 owns this qualitative judgment: what depth the commercial story can
    support without padding.  Timestamp-derived lengths remain the validator's
    job and are reported separately in ``duration_assessment``.
    """

    target_duration: float
    feasible_min_seconds: float
    feasible_max_seconds: float
    recommended_duration: float
    duration_status: str
    depth_mode: str
    reason: str
    reported: bool = True
    planner_report: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target_duration": round(self.target_duration, 3),
            "feasible_duration_range": {
                "min_seconds": round(self.feasible_min_seconds, 3),
                "max_seconds": round(self.feasible_max_seconds, 3),
            },
            "recommended_duration": round(self.recommended_duration, 3),
            "duration_status": self.duration_status,
            "depth_mode": self.depth_mode,
            "reason": self.reason,
            "reported": self.reported,
        }
        if self.planner_report:
            payload["planner_report"] = dict(self.planner_report)
        return payload


@dataclass(frozen=True)
class DepthExpansionValue:
    """One distinct purchase value that makes a long version deeper, not wider."""

    dimension: str
    chapter_ids: tuple[str, ...]
    purchase_value: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "chapter_ids": list(self.chapter_ids),
            "purchase_value": self.purchase_value,
        }


@dataclass(frozen=True)
class DepthExpansionContract:
    """Director-authored proof that a long plan adds distinct buyer value."""

    base_covered_values: tuple[str, ...]
    new_value_chapters: tuple[DepthExpansionValue, ...]
    same_claim_additional_proof_chapter_ids: tuple[str, ...]
    status: str
    reason: str
    reported: bool = True
    planner_report: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "base_covered_values": list(self.base_covered_values),
            "new_value_chapters": [item.to_dict() for item in self.new_value_chapters],
            "same_claim_additional_proof_chapter_ids": list(self.same_claim_additional_proof_chapter_ids),
            "status": self.status,
            "reason": self.reason,
            "reported": self.reported,
        }
        if self.planner_report:
            payload["planner_report"] = dict(self.planner_report)
        return payload


@dataclass(frozen=True)
class DurationExpansionAsset:
    """A director-visible, unselected asset found for one new buyer value.

    This is a recall aid for a second M2 pass.  It deliberately contains no
    position, chapter or selection decision: the Commercial Story Planner
    remains the only component allowed to choose and order candidates.
    """

    candidate_id: int
    value_dimension: str
    candidate_duration: float
    purchase_value: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "value_dimension": self.value_dimension,
            "candidate_duration": round(self.candidate_duration, 3),
            "purchase_value": self.purchase_value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DurationExpansionScout:
    """Auditable recall result before a long-form duration replan."""

    assets: tuple[DurationExpansionAsset, ...]
    status: str
    reason: str
    reported: bool = True
    scout_report: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "assets": [asset.to_dict() for asset in self.assets],
            "status": self.status,
            "reason": self.reason,
            "reported": self.reported,
        }
        if self.scout_report:
            payload["scout_report"] = dict(self.scout_report)
        return payload


@dataclass(frozen=True)
class OpeningQualityReview:
    """M2's focused semantic review of the already-planned opening unit.

    The review has no authority over M1's story, the ledger, or any chapter
    after C2.  It can only ask the same M2 director to re-author the opening
    from the executable candidate pool, then leaves deterministic lineage and
    boundary validation to the existing plan validator and M3.
    """

    status: str
    reason: str
    issues: tuple[str, ...]
    hook_independence: str
    live_process_talk: str
    promise_speed: str
    payoff_relation: str
    compactness: str
    opening_unit_seconds: float
    replanned: bool = False
    review_report: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "issues": list(self.issues),
            "hook_independence": self.hook_independence,
            "live_process_talk": self.live_process_talk,
            "promise_speed": self.promise_speed,
            "payoff_relation": self.payoff_relation,
            "compactness": self.compactness,
            "opening_unit_seconds": round(self.opening_unit_seconds, 3),
            "replanned": self.replanned,
        }
        if self.review_report:
            payload["review_report"] = dict(self.review_report)
        return payload


@dataclass(frozen=True)
class NarrativePlan:
    strategy_id: str
    thesis: str
    target_duration: float
    beats: tuple[NarrativeBeat, ...]
    status: str
    recommended_duration: float
    issues: tuple[str, ...]
    removed_beats: tuple[RemovedBeat, ...]
    plan_valid: bool
    story_brief: CommercialStoryBrief | None = None
    opening_package: OpeningPackage | None = None
    selection_contract: Mapping[str, Any] | None = None
    selected_candidates: tuple[PlanningCandidate, ...] = ()
    replan_request: ReplanRequest | None = None
    duration_assessment: Mapping[str, Any] | None = None
    duration_plan: DurationPlan | None = None
    depth_expansion: DepthExpansionContract | None = None
    duration_expansion_scout: DurationExpansionScout | None = None
    story_consumption: StoryConsumption | None = None
    opening_quality_review: OpeningQualityReview | None = None

    @property
    def total_seconds(self) -> float:
        if self.selected_candidates:
            return round(sum(candidate.duration for candidate in self.selected_candidates), 1)
        return round(sum(beat.target_seconds for beat in self.beats), 1)

    def to_dict(self) -> dict[str, Any]:
        selection_contract = dict(self.selection_contract or {})
        m1_source_story = selection_contract.get("m1_source_story")
        director_narrative_contract = selection_contract.get("director_narrative_contract")
        final_story_brief = selection_contract.get("final_story_brief")
        return {
            "strategy_id": self.strategy_id,
            "thesis": self.thesis,
            "target_duration": self.target_duration,
            "total_seconds": self.total_seconds,
            "status": self.status,
            "recommended_duration": self.recommended_duration,
            "issues": list(self.issues),
            "removed_beats": [beat.to_dict() for beat in self.removed_beats],
            "plan_valid": self.plan_valid,
            # ``story_brief`` remains the frozen M1 discovery record for
            # backwards compatibility.  New director-led reports expose the
            # final narration authority separately so an old M1 Hero cannot
            # masquerade as the story currently being edited.
            "story_brief": self.story_brief.payload() if self.story_brief else None,
            "m1_source_story": dict(m1_source_story) if isinstance(m1_source_story, Mapping) else (self.story_brief.payload() if self.story_brief else None),
            "director_narrative_contract": dict(director_narrative_contract) if isinstance(director_narrative_contract, Mapping) else None,
            "final_story_brief": dict(final_story_brief) if isinstance(final_story_brief, Mapping) else None,
            "opening_package": self.opening_package.to_dict() if self.opening_package else None,
            "selection_contract": selection_contract,
            "selected_candidates": [candidate.payload() for candidate in self.selected_candidates],
            "replan_request": self.replan_request.to_dict() if self.replan_request else None,
            "duration_assessment": dict(self.duration_assessment or {}),
            "duration_plan": self.duration_plan.to_dict() if self.duration_plan else None,
            "depth_expansion": self.depth_expansion.to_dict() if self.depth_expansion else None,
            "duration_expansion_scout": (
                self.duration_expansion_scout.to_dict() if self.duration_expansion_scout else None
            ),
            "story_consumption": self.story_consumption.to_dict() if self.story_consumption else None,
            "opening_quality_review": self.opening_quality_review.to_dict() if self.opening_quality_review else None,
            "beats": [beat.to_dict() for beat in self.beats],
        }


PLANNER_SYSTEM_PROMPT = """你是女装直播短视频的商业导演（Commercial Story Planner）。

你的输入包含：一个 Commercial Story Brief、完整的 hard-safe 候选池、M1 标出的
Core/Supporting/Bridge 资产，以及本次不可变的 Selection Contract。

你的任务不是按卖点打榜，也不是把 Brief 的证据顺序照抄。先理解：这条视频的观众矛盾、
需要发生的转变、产品扮演的角色、最终要兑现的购买感受；再设计一条丰富、有推进的短视频。

规则：
1. 所有候选都是 hard-safe；只能引用真实 candidate_id，不能改写文本、时间、来源或创造候选。
2. Core / Supporting / Bridge 是资产优先级，不是素材围栏。Supporting/Bridge 可以让主故事丰满，
   safe_reserve 也可在确实服务主故事、补充可信证据或满足混剪来源时使用；不可让辅助内容抢走主线。
   你正在执行 Brief 指定的 strategy_id，不得重新发现另一个商业主题（例如把“单穿不职业”改成价格故事）。
   每个章节都要写 story_support，说明它怎样服务这一个主故事；Supporting 只能扩展购买理由，不能替代主轴。
   使用 Bridge 时，story_support 必须写明它连接了主故事的哪两个认知环节，不能把独立场景当作桥。
3. 开场必须输出 opening_package：Hook 是一个承诺，紧接着的 payoff 要解释、展示或推进这个承诺。
   Hook + payoff 是一个开场单元；不要把它们机械写成所有视频相同的固定模板。
   Hook 优选 2.5-5 秒；5-8 秒仅在表达语义完整、没有直播废话，且不能在不损失承诺下再压缩时允许。
   超过 8 秒应拆成 Hook + 紧接的 payoff 或重新导演；不能依赖本地校验替你拆改。
4. 后续章节按故事需要组织，可用 development / proof / scene / contrast / benefit / turn / close 等角色。
   一条约40秒的视频应有层次和变化，但不能为了覆盖卖点硬塞无关内容。不要因为角色名存在，
   就机械输出 proof→benefit→scene→close；每个章节都必须让故事发生新的推进。
   一个核心承诺获得“足够兑现”即可进入新的 Supporting Arc：观众已经听到承诺、理解了机制或
   看见了至少一个可信证明/结果时，不必穷尽所有相近证据再换话题。相邻章节若只是反复证明
   同一个已被充分理解的 claim，应优先改用新的购买价值：新人群、场景、舒适度、信任、风格
   或拥有感；主线仍需始终服务同一个 Commercial Story。
   同一购买价值通常最多连续推进两层（例如“机制→一个量化证明”），在第三层前必须检视完整
   候选池是否已有可用的新价值。一个章节内的多个 candidate 只能构成必要的连续表达，或各自
   提供不同信息；两句只是同义重复同一证明时，保留更具体、更有画面或更短的一句。
   对 Brief 已声明的 supporting_arcs，先在完整 hard-safe 候选池中主动寻找真实证据，再决定
   是否继续追加重复的 Core 证明。supporting_arcs 是召回方向，不是强制章节；若没有完整、相关
   的真实表达，可以不用，但不能因为它尚未出现在 M1 的 supporting_evidence_pool 就忽略全池。
   当 Supporting Arc Recall Index 已列出完整且相关的真实表达时，在核心承诺足够兑现后，优先用
   至少一个这样的新价值丰富故事；只有它与主商品、合同或主故事确实冲突时才可不用。不要一边
   放弃这些候选，一边把计划报成“素材不足”或继续追加重复的 Core 证明。
5. 不要重复 candidate_id。没有合格收尾可自然结束；不要复用前文作总结。候选通过 hard-safe
   只代表内容规则安全，不代表适合作为独立章节：明显残句、无意义重复、依赖缺失上下文的句子
   不能单独做 Hook、收尾或一章。只有与同一连续组的相邻候选共同构成完整表达时才可使用。
   尤其 Hook 本身必须脱离前一句也能成立：不要拿“对/嗯/是的”等回应开头，或主语、对象完全
   缺失的结果句当开场，即使该候选被上游标记为 hook_eligible；它们最多只能在已有完整表达后
   作为同一连续组的正文使用。
   收尾必须增加新的结果、情绪或拥有感；没有新增推进时宁可在上一章自然结束。
6. 目标时长是质量导向的软合同：若目标40秒，优先尝试约31-48秒。先建立开场单元和核心
   转变，再主动在完整 safe_reserve 中寻找1-3段与主故事有关的新证据、场景、风格结果或
   购买代入来丰富推进。不能为了凑时长插无关卖点，也不能只因Core很短就提前放弃。
   只有检视完整安全池后仍没有相关资产，才输出较短计划，并在 duration_assessment 说明缺什么。
7. 章节之间先判断有没有真实的语义桥：例如“结构显瘦→松弛透气→出游场景”，或“场景顾虑
   →面料/设计解决”。有合格 Bridge 候选时可选入作为推进；没有时，让前一小段自然闭环后再
   切换，不能为了连接硬塞泛夸、直播互动或与主故事无关的卖点。每个 C2 及之后的章节都填写
   transition_from_previous，简要说明它怎样承接上章，或写“上一章已闭环，转入相关新推进”。
   Bridge 候选的原话必须实际含有前后主题的共同支点（例如“显瘦又清爽”把身材效果接到面料，
   或“出去玩怕闷→透气”把场景接到材质）。仅仅是另一句场景、颜色或面料介绍不算桥；这时必须
   诚实写“上一章已闭环，转入相关新推进”。这只是导演的审计说明，不是要求每次都新增一条
   Bridge 片段。Bridge 只负责让已有的商业推进更顺，不能因为找不到完美桥就删除、拖后或重排
   原本有价值的 Supporting Arc。场景化延展可在核心承诺足够兑现后进入正文或结尾，只要它带来
   新的购买感受；不要仅为凑时长插入无关场景，也不要在场景后折返到已被穷尽的重复证明。
8. 只输出合法 JSON，不要输出解释性文字或思维过程。
"""


def _asset_label(candidate: PlanningCandidate) -> str:
    tiers = set(candidate.asset_tiers)
    for tier in ASSET_TIERS:
        if tier in tiers:
            return tier
    return "safe_reserve"


def _prompt_candidate_groups(
    candidates: Sequence[PlanningCandidate],
    *,
    supporting_recall_ids: Sequence[int] = (),
) -> tuple[tuple[str, tuple[PlanningCandidate, ...]], ...]:
    """Present every hard-safe candidate, with M1 assets first for attention.

    This is deliberately presentation-only. The director still receives every
    safe reserve candidate and can select it; no group is an allow-list.
    """

    groups: list[tuple[str, tuple[PlanningCandidate, ...]]] = []
    recall_ids = {int(candidate_id) for candidate_id in supporting_recall_ids}
    for tier, title in (
        ("core", "M1 Core assets (priority evidence, not a whitelist)"),
        ("supporting", "M1 Supporting assets (may enrich the main story)"),
        ("bridge", "M1 Bridge assets (may connect or broaden the story)"),
        ("safe_reserve", "Remaining hard-safe reserve (still selectable)"),
    ):
        members = tuple(
            candidate
            for candidate in candidates
            if (tier == "safe_reserve" and not candidate.asset_tiers)
            or (tier != "safe_reserve" and _asset_label(candidate) == tier)
        )
        if members:
            groups.append((title, members))
    recalled_reserve = tuple(
        candidate for candidate in candidates
        if candidate.candidate_id in recall_ids and not candidate.asset_tiers
    )
    if recalled_reserve:
        groups.insert(
            min(3, len(groups)),
            ("M1 Supporting Arc recall (attention guidance, not a whitelist)", recalled_reserve),
        )
    if recall_ids:
        groups = [
            (title, tuple(
                candidate for candidate in members
                if not (title.startswith("Remaining hard-safe reserve") and candidate.candidate_id in recall_ids)
            ))
            for title, members in groups
        ]
        groups = [(title, members) for title, members in groups if members]
    return tuple(groups)


def _format_candidate_pool(
    candidates: Sequence[PlanningCandidate],
    *,
    supporting_recall_ids: Sequence[int] = (),
) -> str:
    lines: list[str] = []
    for title, members in _prompt_candidate_groups(candidates, supporting_recall_ids=supporting_recall_ids):
        lines.append(f"[{title}]")
        for candidate in members:
            lines.append(
                "  - "
                f"id={candidate.candidate_id} tier={_asset_label(candidate)} source={candidate.source_id} "
                f"time={candidate.start:.3f}-{candidate.end:.3f} duration={candidate.duration:.3f}s "
                f"hook_eligible={str(candidate.hook_eligible).lower()} "
                f"story_block={candidate.story_block_id or '-'} text={json.dumps(candidate.text, ensure_ascii=False)}"
            )
    return "\n".join(lines)


def _format_duration_expansion_catalog(
    candidates: Sequence[PlanningCandidate],
    *,
    selected_candidate_ids: Sequence[int],
) -> str:
    """Repeat the unconsumed safe pool beside a duration-expansion request.

    This is retrieval context, not a local ranking or an allow-list.  A second
    M2 pass previously received the full original pool only far above its
    shortfall instructions, which made it prone to echo the old plan.  Keeping
    the actual unused IDs, durations and text next to the request lets the
    director search for a new buyer-value chapter without granting the program
    any selection or ordering authority.
    """

    selected = {int(candidate_id) for candidate_id in selected_candidate_ids}
    remaining = tuple(
        candidate for candidate in candidates
        if candidate.candidate_id not in selected
    )
    if not remaining:
        return "  - [没有未使用的 hard-safe 候选]"
    return "\n".join(
        "  - "
        f"id={candidate.candidate_id} duration={candidate.duration:.3f}s "
        f"source={candidate.source_id} story_block={candidate.story_block_id or '-'} "
        f"text={json.dumps(candidate.text, ensure_ascii=False)}"
        for candidate in remaining
    )


def _format_opening_priority_index(candidates: Sequence[PlanningCandidate]) -> str:
    """Give M1-backed legal Hooks a visible cue without hiding other choices."""

    indexed = tuple(
        candidate
        for candidate in candidates
        if candidate.hook_eligible and candidate.asset_tiers
    )
    if not indexed:
        return "  - [No M1-backed short Hook; inspect the complete pool for a legal fallback.]"
    return "\n".join(
        "  - "
        f"id={candidate.candidate_id} tier={_asset_label(candidate)} duration={candidate.duration:.3f}s "
        f"text={json.dumps(candidate.text, ensure_ascii=False)}"
        for candidate in indexed
    )


def _supporting_arc_terms(arc: str) -> tuple[str, ...]:
    """Return conservative two-plus-character recall hints for one M1 arc.

    This is only an attention index for the director.  It never filters or
    ranks the legal candidate pool, and the director still judges whether any
    listed line is a complete, relevant expression.
    """

    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(arc or ""))
    if len(compact) < 2:
        return ()
    terms = {compact[index:index + width] for width in (2, 3) for index in range(len(compact) - width + 1)}
    return tuple(sorted((term for term in terms if len(term) >= 2), key=lambda term: (-len(term), term)))


def _format_supporting_arc_recall_index(
    brief: CommercialStoryBrief,
    candidates: Sequence[PlanningCandidate],
    *,
    limit_per_arc: int = 6,
) -> str:
    """Expose likely full-pool evidence for M1's named supporting arcs.

    M1 may discover a useful arc without attaching every supporting subtitle.
    The index prevents those arcs from being visually lost in a large reserve
    pool while preserving the director's full, unrestricted candidate choice.
    """

    arcs = tuple(str(arc).strip() for arc in brief.supporting_arcs if str(arc).strip())
    if not arcs:
        return "  - [M1 did not declare a supporting arc.]"
    lines: list[str] = []
    for arc in arcs:
        terms = _supporting_arc_terms(arc)
        scored: list[tuple[int, float, int, PlanningCandidate, tuple[str, ...]]] = []
        for candidate in candidates:
            text = str(candidate.text or "")
            matched = tuple(term for term in terms if term in text)
            if not matched:
                continue
            # Phrase overlap is a retrieval clue only.  Prefer fuller lines
            # when the overlap is identical; do not make a quality decision.
            score = sum(len(term) for term in matched)
            scored.append((-score, -float(candidate.duration), candidate.candidate_id, candidate, matched))
        lines.append(f"  - arc={json.dumps(arc, ensure_ascii=False)}")
        if not scored:
            lines.append("    [no lexical recall; inspect the full pool semantically]")
            continue
        for _score, _duration, _candidate_id, candidate, matched in sorted(scored)[:max(1, int(limit_per_arc))]:
            lines.append(
                "    "
                f"id={candidate.candidate_id} duration={candidate.duration:.3f}s "
                f"matched={json.dumps(list(matched), ensure_ascii=False)} "
                f"text={json.dumps(candidate.text, ensure_ascii=False)}"
            )
    return "\n".join(lines)


def _supporting_arc_recall_ids(
    brief: CommercialStoryBrief,
    candidates: Sequence[PlanningCandidate],
    *,
    limit_per_arc: int = 6,
) -> tuple[int, ...]:
    """Return presentation-only recall IDs for declared supporting arcs."""

    ids: list[int] = []
    for arc in tuple(str(item).strip() for item in brief.supporting_arcs if str(item).strip()):
        terms = _supporting_arc_terms(arc)
        scored: list[tuple[int, float, int]] = []
        for candidate in candidates:
            if candidate.asset_tiers:
                continue
            matched = tuple(term for term in terms if term in str(candidate.text or ""))
            if matched:
                scored.append((-sum(len(term) for term in matched), -float(candidate.duration), candidate.candidate_id))
        for _score, _duration, candidate_id in sorted(scored)[:max(1, int(limit_per_arc))]:
            if candidate_id not in ids:
                ids.append(candidate_id)
    return tuple(ids)


def build_planner_prompt(
    strategy: Strategy,
    target_duration: float,
    safe_candidates: Sequence[PlanningCandidate] = (),
    selection_contract: Mapping[str, Any] | None = None,
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
) -> str:
    """Build the exact M2 prompt without hiding safe reserve candidates."""

    brief = CommercialStoryBrief.from_strategy(strategy)
    candidates = bind_story_assets(brief, tuple(safe_candidates))
    evidence_view = build_executable_evidence_view(brief, candidates, executable_evidence)
    supporting_recall_ids = _supporting_arc_recall_ids(brief, candidates)
    target = max(1.0, float(target_duration))
    preferred_low = round(target * 0.78, 1)
    preferred_high = round(target * 1.20, 1)
    depth_mode = _duration_depth_mode(target)
    depth_instruction = _duration_depth_instruction(depth_mode)
    commerce_story_plan = dict((selection_contract or {}).get("commerce_story_plan") or {})
    commerce_instruction = ""
    if commerce_story_plan:
        available_required_ids = [
            str(item.get("beat_id") or "").strip()
            for item in commerce_story_plan.get("beats") or ()
            if isinstance(item, Mapping) and bool(item.get("required")) and str(item.get("availability") or "").strip() == "available"
        ]
        commerce_instruction = (
            "M2.5 Commerce Story Plan 是先于选片生成的购买认知覆盖合同，不是候选白名单、不是时间顺序、"
            "也不删除完整 hard-safe 池。你仍须自行从完整且 materializable 的候选中选择真实句子。"
            "每个 required 且 availability=available 的 commerce beat 必须由至少一个 chapter 的 commerce_beat_id 显式承接；"
            "一个 chapter 可以承接多个认知，但只能写一个主要 commerce_beat_id。"
            f"本次必须映射的 beat_id={json.dumps(available_required_ids, ensure_ascii=False)}。"
            "若 M2.5 已诚实标记 insufficient_evidence，不得靠重复、补造或新主题伪装覆盖。"
        )
    return "\n".join((
        "Commercial Story Brief:",
        json.dumps(brief.payload(), ensure_ascii=False, indent=2),
        "Executable Evidence View（M1 Brief 保持原样；这是下游词级边界的只读事实）:",
        json.dumps([item.to_dict() for item in evidence_view], ensure_ascii=False, indent=2),
        "每一条 evidence 的 candidate_id 都同时给出 commercial_role、story_tier 与 materializable。"
        "materializable=false 不否定它的商业价值，也不修改 Brief；但本次 Plan 的 opening_package 和 chapters"
        "绝不能引用该 candidate_id。materialization_issue 只说明为什么本轮不能交给 M3。"
        "只可提交 materializable=true 且出现在完整候选池中的 candidate_id；不得用语义相近句替它偷换候选。",
        f"本次只执行 M1 Hero strategy_id={brief.strategy_id}，其 story_priority={brief.story_priority}。"
        "priority 是 M1 的故事重要性声明，不是自动选片或重新打榜依据。",
        "Story Consumption Contract：opening_package、每个 chapter.goal 与 story_support 必须推进该 Brief 的"
        "观众矛盾→解决机制→购买结果。Supporting 只能解释或扩大该主线的购买理由；Bridge 只能连接"
        "两个已存在的主线认知。不得另起价格、库存、其他商品或 Ledger 未支持的新商业主题。",
        (
            "本次是 Consumption Validation：必须实际选择至少一个带 supporting tier 的候选；若 Brief 有 bridge_assets，"
            "必须实际选择至少一个带 bridge tier 的候选。story_consumption 中声明的 supporting_chapter_ids / "
            "bridge_chapter_ids 必须逐一对应真实 tier 的章节，不能把 Core 或 safe_reserve 冒充为 Supporting/Bridge。"
            "supporting_candidate_ids / bridge_candidate_ids 必须分别逐一列出最终实际选中的对应 tier candidate_id；"
            "声明和 chapters 的真实选择不一致会使 Plan 无效。"
            "输出前必须反向核对 chapters：supporting_chapter_ids 只能列出实际选中 supporting tier 候选的章节；"
            "bridge_chapter_ids 只能列出实际选中 bridge tier 候选的章节。"
            if bool((selection_contract or {}).get("m1_consumption_validation_require_supporting_bridge"))
            else ""
        ),
        commerce_instruction,
        "",
        f"目标时长: {target:.1f} 秒；优先实际时长区间: {preferred_low:.1f}-{preferred_high:.1f} 秒（软合同）",
        f"本次叙事深度: {depth_mode}。{depth_instruction}",
        (
            "Depth Expansion Contract：本次目标超过75秒。先把短版已经完整覆盖的价值写入 "
            "base_covered_values（core_promise / mechanism / proof / core_result），再扩展。长版不是证明更多，"
            "而是购买理由更完整。new_value_chapters 中每一项必须绑定一个真实章节，并且 dimension 只能是 "
            "new_audience / new_scene / new_comfort / new_styling_result / new_objection / new_trust / "
            "new_usage_cycle / new_emotional_value。"
            if _depth_expansion_requirement(target)[0]
            else "Depth Expansion Contract：本次为短版，不要求额外价值章节；优先高密度讲完整核心故事。"
        ),
        (
            f"本档长版至少需要 {_depth_expansion_requirement(target)[0]} 个不同的新购买价值，理想为 "
            f"{_depth_expansion_requirement(target)[1]} 个。same_claim_additional_proof 最多允许一章，"
            "它不能承担长版的主要增量。若完整安全池没有足够不同价值，输出 "
            "depth_expansion.status=insufficient_distinct_value，同时 duration_plan.status 必须为 "
            "insufficient_for_target；保留最自然的较短版本，不能用同义证明凑长。"
            if _depth_expansion_requirement(target)[0]
            else ""
        ),
        "Selection Contract（任务快照，只用于本次规划）:",
        json.dumps(dict(selection_contract or {}), ensure_ascii=False, sort_keys=True),
        "",
        "完整 hard-safe 候选池（tier 是优先级，不是准入限制）:",
        _format_candidate_pool(candidates, supporting_recall_ids=supporting_recall_ids) or "  - [EMPTY]",
        "",
        "开场优先索引（仅列出 M1 已关联、hook_eligible=true 且不超过 8 秒的候选；不是白名单，"
        "其余 hard-safe 候选仍可作合法 Hook 或正文）:",
        _format_opening_priority_index(candidates),
        "",
        "Supporting Arc Recall Index（仅帮助你在完整池中先看到 M1 已声明的辅助弧线；"
        "不是白名单、不是排序，也不代表这些候选一定完整或可用）:",
        _format_supporting_arc_recall_index(brief, candidates),
        "",
        "Duration-Aware Planning：先判断本故事在完整安全池中能自然展开到什么深度，再选择章节。"
        "不要把目标秒数当成必须凑满的配额。必须返回 duration_plan：target_duration 必须等于本次目标；"
        "feasible_duration_range 表示这条故事在不填充无关内容时可成立的时长范围；"
        "recommended_duration 是你认为商业质量最佳的时长；若目标高于可自然支撑范围，"
        "duration_status 必须写 insufficient_for_target，而不是添加无关卖点。",
        "提交前的确定性自检：用候选表的 duration 计算所有选中 candidate_id 的真实总时长。"
        f"只有真实总时长落在 {preferred_low:.1f}-{preferred_high:.1f}s，"
        "duration_assessment.status 才能写 full；否则必须写 insufficient_material，"
        "并说明在完整安全池中缺少哪类仍服务主故事的资产。不要把目标时长当成已实现时长。"
        "duration_plan 是导演的可讲深度判断，duration_assessment 是这次实际候选的时间戳审计，两者不得混淆。",
        "若本次计划超出优先上限，只能由你删去整个重复或不推进章节，或改用更短的完整等价表达；"
        "不能截断候选文本，也不能因为每段都不错就全部保留。",
        "核心承诺得到足够兑现就可进入新的购买价值：已经有承诺、机制或可信证明/结果后，不必穷尽"
        "同类证据。相邻章节不要反复证明已充分理解的同一 claim；优先扩展新人群、场景、舒适度、"
        "信任、风格或拥有感，但全部仍服务同一个 Commercial Story。",
        "信息增量检查：同一购买价值通常最多连续推进两层（如机制→一个量化证明）；第三层前先检视"
        "完整候选池是否已有可用的新价值。一个章节内多个 candidate 必须构成必要连续表达或提供不同"
        "信息；同义重复的证明只保留更具体、更有画面或更短的一句。",
        "辅助弧线召回：对 Brief 已声明的 supporting_arcs，先在完整 hard-safe 候选池中主动寻找真实"
        "证据，再决定是否追加重复的 Core 证明。supporting_arcs 是召回方向，不是强制章节；没有完整"
        "相关表达可以不用，但不能因它未绑定 M1 supporting_evidence_pool 就忽略全池。",
        "若 Supporting Arc Recall Index 已列出完整且相关的真实表达，在核心承诺足够兑现后，优先用至少"
        "一个这样的新价值丰富故事；只有它与主商品、合同或主故事确实冲突时才可不用。不要放弃这些"
        "候选后又报素材不足，或继续追加重复的 Core 证明。",
        "Hook 自足检查：第一条字幕必须脱离前文也能独立成立。不要用“对/嗯/是的”等回应句，或"
        "主语、对象完全缺失的结果句做 Hook，即使候选表标为 hook_eligible；这类句子最多只能在"
        "已有完整表达后作为同一连续组的正文使用。",
        "Hook 时长合同：优选 2.5-5.0 秒；5.0-8.0 秒是软容差，只在 Hook 语义完整、没有明显废话，"
        "且不能在不损失承诺下再压缩时允许，必须填写 hook_integrity_reason。超过 8 秒必须拆成 Hook +"
        "紧接的 payoff 或重新导演，不能等待本地校验替你拆改。",
        "Opening Unit 时长合同：Hook + 紧接 payoff 优选不超过 10 秒；超过 12 秒将进入硬复核。"
        "在前 8-10 秒内应完成被吸引→马上被证明，而不是为了短而牺牲完整承诺。",
        "Opening Contract：opening_package 不是两个松散字段。hook_candidate_ids 必须与 C1 完全一致，"
        "payoff_candidate_ids 必须与紧接的 C2 完全一致。hook_promise 写 C1 实际承诺；payoff_delivery 写 C2"
        "如何用其实际候选解释、展示或证明该承诺；connection_reason 必须说明这两个真实候选为什么构成即时"
        "承诺→兑现，而不是两个各说各话的卖点。三项文字和两个 ID 组缺一不可。",
        "章节衔接检查：C2 及之后必须填写 transition_from_previous，说明它如何兑现、因果推进、"
        "场景化延展、对比或在上一章闭环后转入相关新推进。只有真实相关时才选择 Bridge 候选；"
        "Bridge 候选的原话本身要带有前后主题共同支点，不能拿另一句独立场景/颜色/面料介绍冒充桥接；"
        "没有真实桥时请明确写“上一章已闭环，转入相关新推进”。"
        "不要为了填该字段新增低价值或无关片段。Bridge 不改变商业推进顺序：不能因为找不到完美桥"
        "就删除、拖后或重排有价值的 Supporting Arc。场景可在核心承诺足够兑现后进入正文或结尾，"
        "只要带来新的购买感受；不能仅为凑时长插入场景，也不要在场景后折返到已被穷尽的重复证明。",
        "",
        "返回 JSON：",
        "{",
        '  "duration_plan": {"target_duration": 60, "feasible_duration_range": {"min_seconds": 45, "max_seconds": 70},',
        '    "recommended_duration": 60, "duration_status": "feasible" | "insufficient_for_target",',
        '    "depth_mode": "core_dense" | "story_with_support" | "chaptered_story" | "expanded_story",',
        '    "reason": "为什么该故事适合这个时长或为什么素材不足"},',
        (
            '  "depth_expansion": {"base_covered_values": ["core_promise", "mechanism", "proof"], '
            '"new_value_chapters": [{"dimension": "new_scene", "chapter_ids": ["C5"], '
            '"purchase_value": "带来新的场景购买理由"}], '
            '"same_claim_additional_proof_chapter_ids": ["C3"], '
            '"status": "expanded" | "insufficient_distinct_value", '
            '"reason": "长版新增了哪些不同购买价值，或为什么无法自然扩展"},'
            if _depth_expansion_requirement(target)[0]
            else '  "depth_expansion": {"status": "not_applicable"},'
        ),
        '  "duration_assessment": {"status": "full" | "insufficient_material", "reason": "若短于优先区间，说明完整安全池中缺少什么相关资产"},',
        '  "opening_package": {',
        '    "hook_promise": "开场承诺",',
        '    "payoff_delivery": "第二段怎样用真实候选兑现",',
        '    "connection_reason": "为什么这两个真实候选构成即时承诺到兑现",',
        '    "hook_integrity_reason": "若 Hook 超过5秒，说明为何不能不损失承诺地再压缩；否则留空",',
        '    "hook_candidate_ids": [101],',
        '    "payoff_candidate_ids": [102],',
        '    "selection_instruction": "开场单元的选择理由"',
        "  },",
        '  "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", '
        '"hero_consistency_reason": "Hook 到章节怎样持续兑现同一主故事", '
        '"supporting_chapter_ids": ["C3"], "bridge_chapter_ids": ["C4"], '
        '"supporting_candidate_ids": [103], "bridge_candidate_ids": [104], "no_rediscovery": true},',
        '  "chapters": [',
        '    {"chapter_id": "C1", "source_role": "hook", "narrative_role": "hook",',
        '     "goal": "建立承诺", "candidate_ids": [101], "required": true,',
        '     "selection_instruction": "为什么是这个开场", "asset_tier": "core",',
        '     "transition_from_previous": "", "value_dimension": "core_promise", '
        '"story_support": "把主故事的核心顾虑抛给观众", "commerce_beat_id": "problem_or_benefit"},',
        '    {"chapter_id": "C2", "source_role": "proof", "narrative_role": "development",',
        '     "goal": "立即兑现并向主故事推进", "candidate_ids": [102], "required": true,',
        '     "selection_instruction": "兑现关系", "asset_tier": "core",',
        '     "transition_from_previous": "用结构解释开场承诺，完成立即兑现", "value_dimension": "mechanism", '
        '"story_support": "直接解释为何主故事承诺成立", "commerce_beat_id": "difference"}',
        "  ]",
        "}",
    ))


def _post_planner_request(
    *,
    api_key: str,
    base_url: str,
    model: str,
    user_prompt: str,
    max_tokens: int = 4000,
    timeout: int = 180,
    stage: str = "M2_story_planner",
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "top_p": 0.8,
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
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=create_ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        record_ai_call(
            module="story_planner", stage=stage, model=model, request_payload=body,
            success=False, error_type=f"http_{error.code}",
        )
        raise RuntimeError(f"Planner HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(
            module="story_planner", stage=stage, model=model, request_payload=body,
            success=False, error_type=type(error).__name__,
        )
        raise RuntimeError(f"Planner 网络错误: {error}") from error
    record_ai_call(
        module="story_planner", stage=stage, model=model, request_payload=body,
        response_payload=result, success=True,
    )

    content = str(result.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    if not content:
        raise RuntimeError("Planner 返回空内容")
    return content


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
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise RuntimeError("Planner 返回无法解析为 JSON")


def _parse_opening_package(raw: Any) -> OpeningPackage | None:
    if not isinstance(raw, Mapping):
        return None
    return OpeningPackage(
        promise=str(raw.get("promise") or raw.get("hook_promise") or "").strip(),
        payoff_relation=str(raw.get("payoff_relation") or raw.get("proof_relation") or "").strip(),
        hook_candidate_ids=_as_int_tuple(raw.get("hook_candidate_ids") or raw.get("hook_ids")),
        payoff_candidate_ids=_as_int_tuple(raw.get("payoff_candidate_ids") or raw.get("payoff_ids")),
        selection_instruction=str(raw.get("selection_instruction") or "").strip(),
        hook_promise=str(raw.get("hook_promise") or raw.get("promise") or "").strip(),
        payoff_delivery=str(raw.get("payoff_delivery") or raw.get("payoff_relation") or raw.get("proof_relation") or "").strip(),
        connection_reason=str(raw.get("connection_reason") or raw.get("payoff_relation") or raw.get("proof_relation") or "").strip(),
        hook_integrity_reason=str(raw.get("hook_integrity_reason") or "").strip(),
    )


def _opening_unit_seconds(
    opening: OpeningPackage | None,
    safe_candidates: Sequence[PlanningCandidate],
) -> float:
    if opening is None:
        return 0.0
    by_id = {candidate.candidate_id: candidate for candidate in safe_candidates}
    return round(sum(
        by_id[candidate_id].duration
        for candidate_id in (*opening.hook_candidate_ids, *opening.payoff_candidate_ids)
        if candidate_id in by_id
    ), 3)


def _regenerate_opening_metadata(
    opening: OpeningPackage,
    safe_candidates: Sequence[PlanningCandidate],
) -> OpeningPackage:
    """Drop stale opening-only metadata after an M2 localized replan.

    A sub-five-second Hook does not need a retained integrity exception.  For a
    soft-tolerance Hook, the new director response must supply a fresh reason;
    no description from the old opening is ever carried across.
    """

    by_id = {candidate.candidate_id: candidate for candidate in safe_candidates}
    hook_seconds = sum(
        by_id[candidate_id].duration
        for candidate_id in opening.hook_candidate_ids
        if candidate_id in by_id
    )
    return replace(
        opening,
        hook_integrity_reason=(
            opening.hook_integrity_reason.strip()
            if hook_seconds > HOOK_PREFERRED_MAX_SECONDS else ""
        ),
    )


def _parse_opening_quality_review(
    raw: Any,
    *,
    opening_unit_seconds: float,
    replanned: bool = False,
) -> OpeningQualityReview:
    data = dict(raw or {}) if isinstance(raw, Mapping) else {}
    status = str(data.get("quality_status") or data.get("status") or "unreported").strip().lower()
    if status not in {
        "pass", "replan", "review_required", "unreported", "invalid_replan",
        "opening_replan_failed_no_better_opening",
    }:
        status = "unreported"
    issues = _as_text_tuple(data.get("issues") or data.get("quality_issues"))
    return OpeningQualityReview(
        status=status,
        reason=str(data.get("reason") or "").strip(),
        issues=issues,
        hook_independence=str(data.get("hook_independence") or "").strip(),
        live_process_talk=str(data.get("live_process_talk") or "").strip(),
        promise_speed=str(data.get("promise_speed") or "").strip(),
        payoff_relation=str(data.get("payoff_relation") or "").strip(),
        compactness=str(data.get("compactness") or "").strip(),
        opening_unit_seconds=opening_unit_seconds,
        replanned=replanned,
        review_report=data,
    )


def _parse_duration_assessment(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    status = str(raw.get("status") or "").strip().lower()
    reason = str(raw.get("reason") or "").strip()
    return {
        key: value
        for key, value in (("status", status), ("reason", reason))
        if value
    }


def _parse_story_consumption(raw: Any) -> StoryConsumption | None:
    if not isinstance(raw, Mapping):
        return None
    return StoryConsumption(
        hero_strategy_id=str(raw.get("hero_strategy_id") or "").strip(),
        hero_priority=str(raw.get("hero_priority") or "").strip().lower(),
        hero_consistency_reason=str(raw.get("hero_consistency_reason") or "").strip(),
        supporting_chapter_ids=_as_identifier_tuple(raw.get("supporting_chapter_ids")),
        bridge_chapter_ids=_as_identifier_tuple(raw.get("bridge_chapter_ids")),
        no_rediscovery=bool(raw.get("no_rediscovery", False)),
        supporting_candidate_ids=_as_int_tuple(raw.get("supporting_candidate_ids")),
        bridge_candidate_ids=_as_int_tuple(raw.get("bridge_candidate_ids")),
    )


def _parse_beats(data: Mapping[str, Any]) -> tuple[NarrativeBeat, ...]:
    chapters = data.get("chapters") or data.get("beats") or ()
    if isinstance(chapters, Mapping):
        chapters = (chapters,)
    beats: list[NarrativeBeat] = []
    for index, item in enumerate(chapters, 1):
        if not isinstance(item, Mapping):
            continue
        candidate_ids = _as_int_tuple(
            item.get("candidate_ids") or item.get("candidate_evidence") or item.get("subtitle_ids")
        )
        asset_tier = str(item.get("asset_tier") or "").strip().lower()
        beats.append(NarrativeBeat(
            source_role=str(item.get("source_role") or "").strip().lower(),
            narrative_role=str(item.get("narrative_role") or item.get("role") or "").strip().lower(),
            goal=str(item.get("goal") or "").strip(),
            candidate_evidence=candidate_ids,
            required=bool(item.get("required", True)),
            target_seconds=round(max(0.0, _number(item.get("target_seconds"))), 1),
            selection_instruction=str(item.get("selection_instruction") or "").strip(),
            chapter_id=str(item.get("chapter_id") or f"C{index}").strip() or f"C{index}",
            asset_tier=asset_tier if asset_tier in ASSET_TIERS else "",
            selection_origin=str(item.get("selection_origin") or "").strip(),
            transition_from_previous=str(item.get("transition_from_previous") or "").strip(),
            value_dimension=str(item.get("value_dimension") or "").strip().lower(),
            purchase_value_dimension=str(item.get("purchase_value_dimension") or "").strip().lower(),
            purchase_value_domain=str(item.get("purchase_value_domain") or "").strip().lower(),
            purchase_value_outcomes=_as_text_tuple(item.get("purchase_value_outcomes")),
            purchase_value_reason=str(item.get("purchase_value_reason") or "").strip(),
            story_support=str(item.get("story_support") or "").strip(),
            commerce_beat_id=str(item.get("commerce_beat_id") or "").strip(),
        ))
    return tuple(beats)


def _plan_duration_status(total: float, target: float) -> str:
    if total > target * 1.20:
        return PLAN_STATUS_ACCEPTABLE_LONG
    if total >= target * 0.93:
        return PLAN_STATUS_OK
    if total >= target * 0.78:
        return PLAN_STATUS_ACCEPTABLE_SHORT
    return PLAN_STATUS_INSUFFICIENT_MATERIAL


def _duration_preferred_range(target: float) -> tuple[float, float]:
    normalized = max(1.0, float(target or 1.0))
    return round(normalized * 0.78, 3), round(normalized * 1.20, 3)


def commerce_lite_story_budget(target_duration: float) -> dict[str, Any]:
    """Return the controlled Lite composition budget for one target duration.

    This is an M2.5 experiment-only *plan audit*: it never removes a
    candidate or makes an alternate selection.  A short story remains valid;
    the upper budget merely prevents a planner from padding a target-length
    commercial story with unlimited supporting information.
    """
    target = max(1.0, float(target_duration or 1.0))
    scale = target / 60.0
    return {
        "target_seconds": round(target, 3),
        "maximum_planned_seconds": round(target + max(5.0, target / 12.0), 3),
        "maximum_chapters": 7,
        "maximum_candidates": 16,
        "primary_story_min_share": 0.50,
        "auxiliary_story_max_share": 0.50,
        "maximum_auxiliary_chapter_seconds": round(target * 0.15, 3),
        "bucket_max_seconds": {
            "hero_problem_solution": round(35.0 * scale, 3),
            "body_extension": round(12.0 * scale, 3),
            "comfort": round(8.0 * scale, 3),
            "trust": round(5.0 * scale, 3),
            "size": round(5.0 * scale, 3),
            "scene": round(5.0 * scale, 3),
            "durability": round(5.0 * scale, 3),
        },
        "candidate_limit_by_bucket": {
            "hero_problem_solution": 3,
            "body_extension": 2,
            "comfort": 1,
            "trust": 1,
            "size": 1,
            "scene": 1,
            "durability": 1,
            "other_auxiliary": 1,
        },
    }


def commerce_lite_chapter_saturation_budget(target_duration: float) -> dict[str, dict[str, float | int]]:
    """Return M2.4's per-chapter stopping limits.

    These are audit limits for a submitted Final plan, never local clipping
    instructions.  The planner remains responsible for deciding which single
    proof completes a chapter and which missing buyer value merits an added
    chapter.
    """
    scale = max(1.0, float(target_duration or 1.0)) / 60.0
    def cap(candidates: int, seconds: float) -> dict[str, float | int]:
        return {"max_candidates": candidates, "max_seconds": round(seconds * scale, 3)}
    return {
        "hook": cap(2, 6.0),
        "pain": cap(2, 6.0),
        "mechanism": cap(2, 7.0),
        "payoff": cap(2, 7.0),
        "visible_result": cap(2, 7.0),
        "body_extension": cap(2, 9.0),
        "risk_reduction": cap(1, 6.0),
        "comfort": cap(1, 6.0),
        "trust": cap(1, 5.0),
        "scene": cap(1, 5.0),
        "scene_or_audience": cap(1, 5.0),
        "default": cap(1, 6.0),
    }


def _commerce_lite_budget_bucket(index: int, beat: NarrativeBeat, safe_map: Mapping[int, PlanningCandidate]) -> str:
    """Classify selected evidence for budget auditing, never for selection.

    Core M1 evidence may occupy the first three problem/mechanism/result
    chapters.  Later body effects are body extensions; all other domains have
    a deliberately compact supporting-information budget.
    """
    selected_tiers = {
        tier
        for candidate_id in beat.candidate_ids
        for tier in tuple((safe_map.get(candidate_id).asset_tiers if safe_map.get(candidate_id) else ()) or ())
    }
    domain = str(beat.purchase_value_domain or "").strip().lower()
    if index < 3 and "core" in selected_tiers:
        return "hero_problem_solution"
    if domain in {"body_appearance", "body_shape", "fit", "silhouette"}:
        return "body_extension"
    if domain in {"comfort", "thermal_comfort"}:
        return "comfort"
    if domain in {"trust", "safety", "quality"}:
        return "trust"
    if domain in {"size", "size_inclusion", "fit_range"}:
        return "size"
    if domain in {"scene", "styling", "lifestyle"}:
        return "scene"
    if domain in {"durability", "care"}:
        return "durability"
    return "other_auxiliary"


def _duration_depth_mode(target: float) -> str:
    """Map the requested runtime to a story depth, never to a filler quota."""

    normalized = max(1.0, float(target or 1.0))
    if normalized <= 45.0:
        return DURATION_DEPTH_CORE_DENSE
    if normalized <= 75.0:
        return DURATION_DEPTH_STORY_WITH_SUPPORT
    if normalized <= 120.0:
        return DURATION_DEPTH_CHAPTERED_STORY
    return DURATION_DEPTH_EXPANDED_STORY


def _duration_depth_instruction(depth_mode: str) -> str:
    instructions = {
        DURATION_DEPTH_CORE_DENSE: "只保留核心承诺、立即兑现和最有区分度的少量证据；不为覆盖卖点加章节。",
        DURATION_DEPTH_STORY_WITH_SUPPORT: "完成主故事后，可加入一到两个真正推进购买理由的 Supporting Arc。",
        DURATION_DEPTH_CHAPTERED_STORY: "完成主故事并章节化展开，可加入二到四个 Supporting 或 Bridge Arc；每章必须有信息增量。",
        DURATION_DEPTH_EXPANDED_STORY: "优先拆成多个完整小段落；若全文没有足够新价值，诚实报告素材不足，不延长重复证明。",
    }
    return instructions[depth_mode]


def _depth_expansion_requirement(target: float) -> tuple[int, int]:
    """Return minimum and preferred new buyer-value chapters for long plans.

    The minimum protects against a 90-second version becoming a longer proof
    reel.  The preferred count is intentionally advisory: a story with only
    one complete new value must report that limit rather than invent a second.
    """
    normalized = max(1.0, float(target or 1.0))
    if normalized <= 75.0:
        return 0, 0
    if normalized <= 105.0:
        return 1, 2
    return 2, 3


def _fallback_depth_expansion(
    target: float,
    *,
    reason: str,
    planner_report: Mapping[str, Any] | None = None,
) -> DepthExpansionContract:
    required, _preferred = _depth_expansion_requirement(target)
    status = DEPTH_EXPANSION_NOT_APPLICABLE if not required else DEPTH_EXPANSION_INSUFFICIENT_DISTINCT_VALUE
    return DepthExpansionContract(
        base_covered_values=(),
        new_value_chapters=(),
        same_claim_additional_proof_chapter_ids=(),
        status=status,
        reason=reason,
        reported=not required,
        planner_report=planner_report,
    )


def _parse_depth_expansion(raw: Any, *, target_duration: float) -> DepthExpansionContract:
    """Parse a director's long-form expansion audit without re-directing it."""
    target = max(1.0, float(target_duration or 1.0))
    required, _preferred = _depth_expansion_requirement(target)
    if not required:
        return _fallback_depth_expansion(target, reason="本次不是长版，不要求深度扩展合同。")
    if not isinstance(raw, Mapping):
        return _fallback_depth_expansion(target, reason="导演未返回 depth_expansion。")

    original = dict(raw)
    status = str(raw.get("status") or "").strip().lower()
    reason = str(raw.get("reason") or "").strip()
    base = tuple(
        value for value in _as_text_tuple(raw.get("base_covered_values"))
        if value in DEPTH_BASE_VALUE_DIMENSIONS
    )
    raw_proof_chapters = raw.get("same_claim_additional_proof_chapter_ids") or ()
    if isinstance(raw_proof_chapters, str):
        raw_proof_chapters = (raw_proof_chapters,)
    proof_chapters = tuple(
        str(value or "").strip()
        for value in raw_proof_chapters
        if str(value or "").strip()
    )
    values: list[DepthExpansionValue] = []
    malformed = False
    for item in raw.get("new_value_chapters") or ():
        if not isinstance(item, Mapping):
            malformed = True
            continue
        dimension = str(item.get("dimension") or "").strip().lower()
        chapter_ids = tuple(
            str(value or "").strip()
            for value in (item.get("chapter_ids") or ())
            if str(value or "").strip()
        )
        purchase_value = str(item.get("purchase_value") or "").strip()
        if dimension not in DEPTH_NEW_VALUE_DIMENSIONS or not chapter_ids or not purchase_value:
            malformed = True
            continue
        values.append(DepthExpansionValue(dimension, chapter_ids, purchase_value))
    if (
        malformed
        or status not in {DEPTH_EXPANSION_EXPANDED, DEPTH_EXPANSION_INSUFFICIENT_DISTINCT_VALUE}
        or not reason
        or not base
    ):
        return _fallback_depth_expansion(
            target,
            reason="导演返回的 depth_expansion 不完整或不合法。",
            planner_report=original,
        )
    return DepthExpansionContract(
        base_covered_values=base,
        new_value_chapters=tuple(values),
        same_claim_additional_proof_chapter_ids=proof_chapters,
        status=status,
        reason=reason,
        reported=bool(raw.get("reported", True)),
        planner_report=dict(raw.get("planner_report") or {}) if isinstance(raw.get("planner_report"), Mapping) else None,
    )


def _fallback_duration_plan(
    target: float,
    *,
    reason: str,
    planner_report: Mapping[str, Any] | None = None,
) -> DurationPlan:
    preferred_low, preferred_high = _duration_preferred_range(target)
    return DurationPlan(
        target_duration=max(1.0, float(target or 1.0)),
        feasible_min_seconds=preferred_low,
        feasible_max_seconds=preferred_high,
        recommended_duration=max(1.0, float(target or 1.0)),
        duration_status=DURATION_STATUS_UNREPORTED,
        depth_mode=_duration_depth_mode(target),
        reason=reason,
        reported=False,
        planner_report=planner_report,
    )


def _parse_duration_plan(raw: Any, *, target_duration: float) -> DurationPlan:
    """Parse a director duration budget without fabricating a success state.

    The planner may judge feasibility, but it cannot override the immutable task
    target or choose a depth mode inconsistent with the requested duration.
    Malformed/missing data becomes an explicit unreported budget for audit; it
    never causes local candidate insertion, deletion, or reordering.
    """

    target = max(1.0, float(target_duration or 1.0))
    if not isinstance(raw, Mapping):
        return _fallback_duration_plan(target, reason="导演未返回 duration_plan。")
    original = dict(raw)
    interval = raw.get("feasible_duration_range")
    if not isinstance(interval, Mapping):
        return _fallback_duration_plan(target, reason="导演返回的 duration_plan 缺少可讲时长范围。", planner_report=original)
    reported_target = _number(raw.get("target_duration"), -1.0)
    low = _number(interval.get("min_seconds", interval.get("min")), -1.0)
    high = _number(interval.get("max_seconds", interval.get("max")), -1.0)
    recommended = _number(raw.get("recommended_duration"), -1.0)
    status = str(raw.get("duration_status") or raw.get("status") or "").strip().lower()
    depth_mode = str(raw.get("depth_mode") or "").strip().lower()
    expected_depth = _duration_depth_mode(target)
    if (
        abs(reported_target - target) > 0.05
        or low < 0.0
        or high < low
        or recommended < low
        or recommended > high
        or status not in {DURATION_STATUS_FEASIBLE, DURATION_STATUS_INSUFFICIENT_FOR_TARGET}
        or depth_mode != expected_depth
    ):
        return _fallback_duration_plan(
            target,
            reason="导演返回的 duration_plan 与本次目标或时长范围不一致。",
            planner_report=original,
        )
    return DurationPlan(
        target_duration=target,
        feasible_min_seconds=low,
        feasible_max_seconds=high,
        recommended_duration=recommended,
        duration_status=status,
        depth_mode=depth_mode,
        reason=str(raw.get("reason") or "").strip() or "导演未说明时长判断依据。",
        reported=bool(raw.get("reported", True)),
        planner_report=dict(raw.get("planner_report") or {}) if isinstance(raw.get("planner_report"), Mapping) else None,
    )


def duration_plan_needs_refinement(
    plan: NarrativePlan,
    *,
    actual_duration: float | None = None,
) -> bool:
    """Return whether the director's declared budget contradicts its own plan.

    This is an audit trigger, not a local duration repair.  A director that
    says a 120-second story is feasible but only selects 50 seconds must either
    find genuinely relevant additional evidence itself or honestly report that
    the target cannot be met.
    """

    budget = plan.duration_plan
    if not plan.plan_valid or budget is None:
        return False
    if budget.duration_status != DURATION_STATUS_FEASIBLE:
        return False
    actual = plan.total_seconds if actual_duration is None else max(0.0, float(actual_duration))
    tolerance = max(5.0, plan.target_duration * 0.08)
    return (
        actual < budget.feasible_min_seconds - 0.1
        or actual > budget.feasible_max_seconds + 0.1
        or abs(actual - budget.recommended_duration) > tolerance
    )


def finalize_duration_budget_after_retry(plan: NarrativePlan) -> NarrativePlan:
    """Reconcile duration reporting after the director has exhausted its retry.

    This only corrects metadata so a caller cannot advertise a 120-second
    result when the director's final real candidate plan is 46 seconds.  It
    never edits a beat, candidate, timestamp, or chapter ordering.
    """

    budget = plan.duration_plan
    if budget is None:
        return plan
    actual = plan.total_seconds
    preferred_low, preferred_high = _duration_preferred_range(plan.target_duration)
    budget_inconsistent = (
        budget.duration_status == DURATION_STATUS_INSUFFICIENT_FOR_TARGET
        and (
            budget.recommended_duration > actual + 0.1
            or budget.feasible_max_seconds > actual + 0.1
        )
    )
    if not duration_plan_needs_refinement(plan) and not budget_inconsistent:
        return plan

    within_soft_range = preferred_low <= actual <= preferred_high
    final_status = DURATION_STATUS_FEASIBLE if within_soft_range else DURATION_STATUS_INSUFFICIENT_FOR_TARGET
    if within_soft_range:
        feasible_low = budget.feasible_min_seconds
        feasible_high = budget.feasible_max_seconds
        reason = (
            f"导演重规划后按真实候选落为 {actual:.1f}s；处于本次软时长区间，"
            "以实际可落片长度作为推荐时长。"
        )
        audit_status = "recommendation_reconciled_to_actual"
    else:
        feasible_low = actual
        feasible_high = actual
        reason = (
            f"导演已完成一次时长重规划，但按真实候选只能落为 {actual:.1f}s，"
            f"未达到本次软下限 {preferred_low:.1f}s；不能通过填充无关内容补足。"
        )
        audit_status = "insufficient_after_director_retry"
    reconciled = DurationPlan(
        target_duration=plan.target_duration,
        feasible_min_seconds=feasible_low,
        feasible_max_seconds=feasible_high,
        recommended_duration=actual,
        duration_status=final_status,
        depth_mode=_duration_depth_mode(plan.target_duration),
        reason=reason,
        reported=False,
        planner_report=budget.to_dict(),
    )
    assessment = dict(plan.duration_assessment or {})
    assessment["duration_budget_status"] = audit_status
    assessment["duration_refinement_recommended"] = False
    issues = list(plan.issues)
    issue = f"duration_budget_reconciled:actual={actual:.1f}s,status={final_status}"
    if issue not in issues:
        issues.append(issue)
    return replace(
        plan,
        duration_plan=reconciled,
        duration_assessment=assessment,
        issues=tuple(issues),
    )


def _measured_duration_assessment(
    reported: Mapping[str, Any] | None,
    *,
    total: float,
    preferred_low: float,
    preferred_high: float,
) -> dict[str, Any]:
    """Make the displayed duration state derive from immutable timestamps.

    The director's explanation is retained for audit, but it cannot turn a
    29-second selection into a "full" 40-second plan merely by claiming it.
    This adjusts reporting only; it never changes a selected candidate, a beat,
    or its order.
    """

    original = dict(reported or {})
    if total < preferred_low:
        status = PLAN_STATUS_INSUFFICIENT_MATERIAL
        reason = f"按真实候选时间戳，可用故事素材为 {total:.1f}s，低于优先下限 {preferred_low:.1f}s。"
    elif total > preferred_high:
        status = PLAN_STATUS_ACCEPTABLE_LONG
        reason = f"按真实候选时间戳，故事方案为 {total:.1f}s，高于优先上限 {preferred_high:.1f}s。"
    else:
        status = "full"
        reason = f"按真实候选时间戳，故事方案为 {total:.1f}s，位于优先区间 {preferred_low:.1f}-{preferred_high:.1f}s。"
    assessment: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "planned_duration": total,
        "actual_seconds": total,
        "preferred_low": preferred_low,
        "preferred_high": preferred_high,
    }
    if original:
        assessment["planner_report"] = original
    return assessment


def _issue(
    issues: list[str],
    codes: list[str],
    details: list[str],
    affected: list[str],
    code: str,
    detail: str,
    chapter_id: str = "",
) -> None:
    issues.append(f"{code}:{detail}" if detail else code)
    if code not in codes:
        codes.append(code)
    if detail and detail not in details:
        details.append(detail)
    if chapter_id and chapter_id not in affected:
        affected.append(chapter_id)


def validate_narrative_plan(
    plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate] | None = None,
    allowed_evidence_ids: set[int] | None = None,
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
) -> NarrativePlan:
    """Audit a plan without deleting, replacing, trimming, or reordering it.

    ``allowed_evidence_ids`` remains accepted only for old callers.  It is never
    a strategy asset fence: if supplied with a real safe pool it is ignored.
    The live M2 contract requires the complete hard-safe candidate map.
    """

    del allowed_evidence_ids
    safe_map = {candidate.candidate_id: candidate for candidate in safe_candidates or () if candidate.candidate_id > 0}
    materialization_facts = {
        int(candidate_id): dict(fact or {})
        for candidate_id, fact in (executable_evidence or {}).items()
    }

    def materialization_issue(candidate_id: int) -> str:
        fact = materialization_facts.get(candidate_id)
        if not fact or bool(fact.get("materializable", True)):
            return ""
        return str(fact.get("materialization_issue") or fact.get("detail") or "unknown_materialization_issue")

    issues: list[str] = []
    codes: list[str] = []
    details: list[str] = []
    affected: list[str] = []

    if not safe_map:
        _issue(issues, codes, details, affected, "safe_candidate_pool_missing", "M2 requires the full hard-safe candidate pool")
    used_ids: set[int] = set()
    selected_ids: list[int] = []
    for beat in plan.beats:
        chapter_id = beat.chapter_id or beat.narrative_role
        if not beat.candidate_ids:
            _issue(issues, codes, details, affected, "empty_chapter", "chapter has no candidate_id", chapter_id)
            continue
        for candidate_id in beat.candidate_ids:
            candidate = safe_map.get(candidate_id)
            blocked_reason = materialization_issue(candidate_id)
            if blocked_reason:
                _issue(
                    issues, codes, details, affected,
                    "candidate_not_materializable", f"candidate_id={candidate_id}:{blocked_reason}", chapter_id,
                )
                continue
            if candidate is None:
                _issue(
                    issues, codes, details, affected,
                    "unknown_candidate_id", f"candidate_id={candidate_id}", chapter_id,
                )
                continue
            if candidate_id in used_ids:
                _issue(
                    issues, codes, details, affected,
                    "candidate_reuse", f"candidate_id={candidate_id}", chapter_id,
                )
            else:
                used_ids.add(candidate_id)
                selected_ids.append(candidate_id)

    opening = plan.opening_package
    if opening is None:
        _issue(issues, codes, details, affected, "opening_package_missing", "Hook and payoff must be declared together")
    else:
        if not (opening.hook_promise or opening.promise):
            _issue(issues, codes, details, affected, "opening_promise_missing", "opening_package.promise is empty")
        if not (opening.payoff_delivery or opening.payoff_relation):
            _issue(issues, codes, details, affected, "opening_payoff_relation_missing", "opening_package.payoff_relation is empty")
        if not (opening.connection_reason or opening.payoff_relation):
            _issue(issues, codes, details, affected, "opening_connection_reason_missing", "opening_package.connection_reason is empty")
        if not opening.hook_candidate_ids or not opening.payoff_candidate_ids:
            _issue(issues, codes, details, affected, "opening_candidate_ids_missing", "hook and payoff candidate IDs are required")
        for candidate_id in (*opening.hook_candidate_ids, *opening.payoff_candidate_ids):
            blocked_reason = materialization_issue(candidate_id)
            if blocked_reason:
                _issue(
                    issues, codes, details, affected,
                    "opening_candidate_not_materializable", f"candidate_id={candidate_id}:{blocked_reason}",
                )
            if candidate_id not in safe_map:
                _issue(issues, codes, details, affected, "opening_unknown_candidate_id", f"candidate_id={candidate_id}")

    if not plan.beats:
        _issue(issues, codes, details, affected, "empty_plan", "no chapters returned")
    elif opening is not None:
        first = plan.beats[0]
        if first.narrative_role != "hook":
            _issue(issues, codes, details, affected, "opening_hook_chapter_missing", "first chapter must be the declared hook", first.chapter_id)
        if tuple(opening.hook_candidate_ids) != tuple(first.candidate_ids):
            _issue(issues, codes, details, affected, "opening_hook_mismatch", "first chapter does not use declared hook IDs", first.chapter_id)
        for candidate_id in opening.hook_candidate_ids:
            candidate = safe_map.get(candidate_id)
            if candidate and not candidate.hook_eligible:
                _issue(issues, codes, details, affected, "hook_candidate_ineligible", f"candidate_id={candidate_id}", first.chapter_id)
        hook_duration = sum(safe_map[candidate_id].duration for candidate_id in opening.hook_candidate_ids if candidate_id in safe_map)
        if hook_duration > HOOK_ACCEPTABLE_MAX_SECONDS:
            _issue(
                issues, codes, details, affected, "hook_actual_duration_exceeds_8s",
                f"duration={hook_duration:.3f}s", first.chapter_id,
            )
        elif hook_duration > HOOK_PREFERRED_MAX_SECONDS:
            # This is intentionally not a length failure.  The planner owns
            # the commercial judgment; it must merely record why this complete
            # frozen expression cannot be shortened without losing its promise.
            if not opening.hook_integrity_reason:
                _issue(
                    issues, codes, details, affected, "hook_soft_tolerance_rationale_missing",
                    f"duration={hook_duration:.3f}s requires hook_integrity_reason", first.chapter_id,
                )
            else:
                issues.append(
                    f"hook_duration_soft_tolerance:actual={hook_duration:.3f}s,preferred_max={HOOK_PREFERRED_MAX_SECONDS:.1f}s"
                )
        elif 0.0 < hook_duration < HOOK_PREFERRED_MIN_SECONDS:
            issues.append(
                f"hook_duration_below_preferred:actual={hook_duration:.3f}s,preferred_min={HOOK_PREFERRED_MIN_SECONDS:.1f}s"
            )

        if len(plan.beats) < 2:
            _issue(issues, codes, details, affected, "opening_payoff_chapter_missing", "opening needs a payoff chapter")
        else:
            second = plan.beats[1]
            if tuple(opening.payoff_candidate_ids) != tuple(second.candidate_ids):
                _issue(issues, codes, details, affected, "opening_payoff_mismatch", "second chapter does not use declared payoff IDs", second.chapter_id)
            opening_unit_duration = hook_duration + sum(
                safe_map[candidate_id].duration
                for candidate_id in opening.payoff_candidate_ids
                if candidate_id in safe_map
            )
            if opening_unit_duration > OPENING_UNIT_HUMAN_REVIEW_SECONDS:
                issues.append(
                    "opening_quality_human_review_required:"
                    f"actual={opening_unit_duration:.3f}s,review_threshold={OPENING_UNIT_HUMAN_REVIEW_SECONDS:.1f}s"
                )
            elif opening_unit_duration > OPENING_UNIT_WARNING_SECONDS:
                issues.append(
                    "opening_quality_warning:"
                    f"actual={opening_unit_duration:.3f}s,warning_threshold={OPENING_UNIT_WARNING_SECONDS:.1f}s"
                )
            elif opening_unit_duration > OPENING_UNIT_PREFERRED_MAX_SECONDS:
                issues.append(
                    "opening_unit_duration_soft_tolerance:"
                    f"actual={opening_unit_duration:.3f}s,preferred_max={OPENING_UNIT_PREFERRED_MAX_SECONDS:.1f}s"
                )

    if bool((plan.selection_contract or {}).get("commerce_lite_purchase_value_progression")):
        # Lite is allowed to use familiar structural chapter labels, but those
        # labels cannot masquerade as incremental buyer value.  This validates
        # only the model's declaration against its own chapter sequence; it
        # never supplies a substitute candidate, claim, or order.
        established_outcomes: set[str] = set()
        body_outcomes: set[str] = set()
        same_claim_proof_count = 0
        for index, beat in enumerate(plan.beats):
            relation = str(beat.purchase_value_dimension or "").strip().lower()
            domain = str(beat.purchase_value_domain or "").strip().lower()
            outcomes = tuple(sorted({
                str(value).strip().lower()
                for value in beat.purchase_value_outcomes
                if str(value).strip()
            }))
            reason = str(beat.purchase_value_reason or "").strip()
            if relation not in {"new_outcome", "same_claim_additional_proof"}:
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_purchase_value_relation_invalid",
                    f"chapter_id={beat.chapter_id},relation={relation or 'missing'}", beat.chapter_id,
                )
            if not domain:
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_purchase_value_domain_missing",
                    f"chapter_id={beat.chapter_id}", beat.chapter_id,
                )
            if not outcomes:
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_purchase_value_outcomes_missing",
                    f"chapter_id={beat.chapter_id}", beat.chapter_id,
                )
            if not reason:
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_purchase_value_reason_missing",
                    f"chapter_id={beat.chapter_id}", beat.chapter_id,
                )
            if relation == "same_claim_additional_proof":
                same_claim_proof_count += 1
                if outcomes and not set(outcomes).intersection(established_outcomes):
                    _issue(
                        issues, codes, details, affected,
                        "commerce_lite_same_claim_proof_outcome_not_established",
                        f"chapter_id={beat.chapter_id},outcomes={list(outcomes)}", beat.chapter_id,
                    )
            elif outcomes:
                # C1+C2 are the opening promise and immediate delivery.  They
                # may share an outcome.  From C3 onward, only an explicit
                # proof beat may revisit an established purchase outcome.
                repeated = set(outcomes).intersection(established_outcomes)
                if index >= 2 and repeated:
                    _issue(
                        issues, codes, details, affected,
                        "commerce_lite_purchase_outcome_repeated",
                        f"chapter_id={beat.chapter_id},outcomes={sorted(repeated)}", beat.chapter_id,
                    )
                if index >= 2:
                    body_outcomes.update(outcomes)
            established_outcomes.update(outcomes)
        if same_claim_proof_count > 1:
            _issue(
                issues, codes, details, affected,
                "commerce_lite_same_claim_proof_overused",
                f"count={same_claim_proof_count}",
            )
        body_count = max(0, len(plan.beats) - 2)
        required_distinct_body_values = min(3, body_count)
        if body_count and len(body_outcomes) < required_distinct_body_values:
            _issue(
                issues, codes, details, affected,
                "commerce_lite_distinct_purchase_value_insufficient",
                f"actual={len(body_outcomes)},required={required_distinct_body_values}",
            )

    selected_candidates = tuple(safe_map[candidate_id] for candidate_id in selected_ids if candidate_id in safe_map)
    selected_total = round(sum(candidate.duration for candidate in selected_candidates), 1)
    lite_budget_summary: dict[str, Any] | None = None
    if bool((plan.selection_contract or {}).get("commerce_lite_story_budget")):
        # This audit gives Lite a real editing budget.  It checks only the
        # model's submitted chapter IDs and immutable durations; it does not
        # shorten, merge, remove, or replace any candidate locally.
        budget = commerce_lite_story_budget(plan.target_duration)
        bucket_seconds: dict[str, float] = {}
        bucket_chapter_ids: dict[str, list[str]] = {}
        chapter_rows: list[dict[str, Any]] = []
        for index, beat in enumerate(plan.beats):
            chapter_id = beat.chapter_id or f"C{index + 1}"
            duration = round(sum(
                safe_map[candidate_id].duration
                for candidate_id in beat.candidate_ids
                if candidate_id in safe_map
            ), 3)
            bucket = _commerce_lite_budget_bucket(index, beat, safe_map)
            bucket_seconds[bucket] = round(bucket_seconds.get(bucket, 0.0) + duration, 3)
            bucket_chapter_ids.setdefault(bucket, []).append(chapter_id)
            chapter_rows.append({
                "chapter_id": chapter_id,
                "bucket": bucket,
                "seconds": duration,
                "candidate_count": len(beat.candidate_ids),
            })
            candidate_limit = int((budget["candidate_limit_by_bucket"] or {}).get(bucket, 1))
            if len(beat.candidate_ids) > candidate_limit:
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_chapter_claim_density_exceeded",
                    f"chapter_id={chapter_id},bucket={bucket},candidate_count={len(beat.candidate_ids)},max={candidate_limit}",
                    chapter_id,
                )
            if bucket != "hero_problem_solution" and duration > float(budget["maximum_auxiliary_chapter_seconds"]):
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_auxiliary_chapter_budget_exceeded",
                    f"chapter_id={chapter_id},bucket={bucket},actual={duration:.3f}s,max={float(budget['maximum_auxiliary_chapter_seconds']):.3f}s",
                    chapter_id,
                )
        if len(plan.beats) > int(budget["maximum_chapters"]):
            _issue(
                issues, codes, details, affected,
                "commerce_lite_story_budget_chapter_count_exceeded",
                f"actual={len(plan.beats)},max={int(budget['maximum_chapters'])}",
            )
        if len(selected_candidates) > int(budget["maximum_candidates"]):
            _issue(
                issues, codes, details, affected,
                "commerce_lite_story_budget_candidate_count_exceeded",
                f"actual={len(selected_candidates)},max={int(budget['maximum_candidates'])}",
            )
        for bucket, maximum in dict(budget["bucket_max_seconds"] or {}).items():
            actual = float(bucket_seconds.get(bucket, 0.0))
            if actual > float(maximum):
                for chapter_id in bucket_chapter_ids.get(bucket, ()):  # surface every over-budget chapter for M2
                    if chapter_id not in affected:
                        affected.append(chapter_id)
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_story_budget_bucket_exceeded",
                    f"bucket={bucket},actual={actual:.3f}s,max={float(maximum):.3f}s",
                )
        primary_seconds = round(
            float(bucket_seconds.get("hero_problem_solution", 0.0))
            + float(bucket_seconds.get("body_extension", 0.0)),
            3,
        )
        auxiliary_seconds = round(max(0.0, selected_total - primary_seconds), 3)
        primary_share = round(primary_seconds / selected_total, 4) if selected_total else 0.0
        auxiliary_share = round(auxiliary_seconds / selected_total, 4) if selected_total else 0.0
        if selected_total > float(budget["maximum_planned_seconds"]):
            _issue(
                issues, codes, details, affected,
                "commerce_lite_story_budget_duration_exceeded",
                f"actual={selected_total:.3f}s,max={float(budget['maximum_planned_seconds']):.3f}s",
            )
        if selected_total and (
            primary_share < float(budget["primary_story_min_share"])
            or auxiliary_share > float(budget["auxiliary_story_max_share"])
        ):
            _issue(
                issues, codes, details, affected,
                "commerce_lite_story_imbalance",
                f"primary_share={primary_share:.3f},auxiliary_share={auxiliary_share:.3f}",
            )
        lite_budget_summary = {
            "contract": budget,
            "selected_total_seconds": selected_total,
            "primary_story_seconds": primary_seconds,
            "auxiliary_seconds": auxiliary_seconds,
            "primary_story_share": primary_share,
            "auxiliary_share": auxiliary_share,
            "bucket_seconds": bucket_seconds,
            "chapters": chapter_rows,
        }
    chapter_saturation_summary: dict[str, Any] | None = None
    if bool((plan.selection_contract or {}).get("commerce_lite_chapter_saturation")):
        saturation = commerce_lite_chapter_saturation_budget(plan.target_duration)
        chapter_rows: list[dict[str, Any]] = []
        for index, beat in enumerate(plan.beats):
            chapter_id = beat.chapter_id or f"C{index + 1}"
            role = str(beat.narrative_role or "").strip().lower()
            limit = dict(saturation.get(role) or saturation["default"])
            duration = round(sum(
                safe_map[candidate_id].duration
                for candidate_id in beat.candidate_ids
                if candidate_id in safe_map
            ), 3)
            chapter_rows.append({
                "chapter_id": chapter_id,
                "role": role,
                "candidate_count": len(beat.candidate_ids),
                "seconds": duration,
                "max_candidates": int(limit["max_candidates"]),
                "max_seconds": float(limit["max_seconds"]),
            })
            if len(beat.candidate_ids) > int(limit["max_candidates"]):
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_chapter_saturation_candidate_exceeded",
                    f"chapter_id={chapter_id},role={role},actual={len(beat.candidate_ids)},max={int(limit['max_candidates'])}",
                    chapter_id,
                )
            if duration > float(limit["max_seconds"]):
                _issue(
                    issues, codes, details, affected,
                    "commerce_lite_chapter_saturation_duration_exceeded",
                    f"chapter_id={chapter_id},role={role},actual={duration:.3f}s,max={float(limit['max_seconds']):.3f}s",
                    chapter_id,
                )
        chapter_saturation_summary = {"contract": saturation, "chapters": chapter_rows}
    if bool((plan.selection_contract or {}).get("m1_consumption_validation_require_supporting_bridge")):
        consumption = plan.story_consumption
        actual_bridge_ids = tuple(sorted({
            candidate.candidate_id for candidate in selected_candidates if "bridge" in candidate.asset_tiers
        }))
        executable_bridge_available = any("bridge" in candidate.asset_tiers for candidate in safe_map.values())
        if consumption is None:
            _issue(issues, codes, details, affected, "story_consumption_missing", "M2 must declare selected tier candidate IDs")
        else:
            declared_bridge_ids = tuple(sorted(set(consumption.bridge_candidate_ids)))
            if declared_bridge_ids != actual_bridge_ids:
                _issue(
                    issues, codes, details, affected, "bridge_not_consumed",
                    f"declared={list(declared_bridge_ids)},actual={list(actual_bridge_ids)}",
                )
            if executable_bridge_available and not actual_bridge_ids:
                _issue(issues, codes, details, affected, "bridge_not_consumed", "executable bridge asset was not selected")
    total = selected_total
    target = max(1.0, float(plan.target_duration or 1.0))
    preferred_low, preferred_high = _duration_preferred_range(target)
    status = _plan_duration_status(total, target)
    measured_assessment = _measured_duration_assessment(
        plan.duration_assessment,
        total=total,
        preferred_low=preferred_low,
        preferred_high=preferred_high,
    )
    # Keep the Completion Pass provenance at the top level through the later
    # deterministic execution-metadata validation. It never affects selection.
    if isinstance(plan.duration_assessment, Mapping):
        for audit_key in (
            "commerce_lite_completion",
            "commerce_lite_draft_final",
            "commerce_lite_draft_rank_final",
            "commerce_lite_chapter_compression",
            "commerce_lite_final_editor",
        ):
            if audit_key in plan.duration_assessment:
                measured_assessment[audit_key] = plan.duration_assessment[audit_key]
    if lite_budget_summary is not None:
        measured_assessment["commerce_lite_story_budget"] = lite_budget_summary
    if chapter_saturation_summary is not None:
        measured_assessment["commerce_lite_chapter_saturation"] = chapter_saturation_summary
    normalized_duration_plan = (
        _parse_duration_plan(plan.duration_plan.to_dict(), target_duration=target)
        if plan.duration_plan is not None
        else _fallback_duration_plan(target, reason="当前计划没有导演时长预算。")
    )
    normalized_depth_expansion = (
        _parse_depth_expansion(plan.depth_expansion.to_dict(), target_duration=target)
        if plan.depth_expansion is not None
        else _fallback_depth_expansion(target, reason="当前计划没有导演深度扩展合同。")
    )
    minimum_new_values, preferred_new_values = _depth_expansion_requirement(target)
    if minimum_new_values:
        chapter_map = {beat.chapter_id: beat for beat in plan.beats if beat.chapter_id}
        selected_chapters = {
            beat.chapter_id
            for beat in plan.beats
            if beat.chapter_id and beat.candidate_ids
        }
        if not normalized_depth_expansion.reported:
            _issue(
                issues, codes, details, affected,
                "depth_expansion_unreported",
                normalized_depth_expansion.reason,
            )
        else:
            new_dimensions: set[str] = set()
            claimed_new_chapters: dict[str, str] = {}
            for value in normalized_depth_expansion.new_value_chapters:
                new_dimensions.add(value.dimension)
                for chapter_id in value.chapter_ids:
                    beat = chapter_map.get(chapter_id)
                    if chapter_id not in selected_chapters or beat is None:
                        _issue(
                            issues, codes, details, affected,
                            "depth_expansion_unknown_chapter",
                            f"{value.dimension}:{chapter_id}",
                            chapter_id,
                        )
                        continue
                    previous_dimension = claimed_new_chapters.setdefault(chapter_id, value.dimension)
                    if previous_dimension != value.dimension:
                        _issue(
                            issues, codes, details, affected,
                            "depth_expansion_chapter_reclassified",
                            f"{chapter_id}:{previous_dimension}->{value.dimension}",
                            chapter_id,
                        )
                    if beat.value_dimension != value.dimension:
                        _issue(
                            issues, codes, details, affected,
                            "depth_expansion_value_dimension_mismatch",
                            f"{chapter_id}: expected={value.dimension}, actual={beat.value_dimension or 'missing'}",
                            chapter_id,
                        )
            proof_chapters = normalized_depth_expansion.same_claim_additional_proof_chapter_ids
            if len(set(proof_chapters)) > 1:
                _issue(
                    issues, codes, details, affected,
                    "depth_expansion_same_claim_proof_excess",
                    "长版最多允许一章同一 claim 的额外证明。",
                )
            for chapter_id in proof_chapters:
                beat = chapter_map.get(chapter_id)
                if chapter_id not in selected_chapters or beat is None:
                    _issue(
                        issues, codes, details, affected,
                        "depth_expansion_unknown_proof_chapter",
                        chapter_id,
                        chapter_id,
                    )
                elif chapter_id in claimed_new_chapters:
                    _issue(
                        issues, codes, details, affected,
                        "depth_expansion_proof_claim_conflict",
                        f"{chapter_id} 不能同时是新增购买价值和同义证明。",
                        chapter_id,
                    )
                elif beat.value_dimension not in {"same_claim_additional_proof", "proof"}:
                    _issue(
                        issues, codes, details, affected,
                        "depth_expansion_proof_dimension_mismatch",
                        f"{chapter_id}: actual={beat.value_dimension or 'missing'}",
                        chapter_id,
                    )
            if normalized_depth_expansion.status == DEPTH_EXPANSION_EXPANDED:
                if len(new_dimensions) < minimum_new_values:
                    _issue(
                        issues, codes, details, affected,
                        "depth_expansion_insufficient_new_value",
                        f"需要至少{minimum_new_values}个不同新增购买价值，实际{len(new_dimensions)}个。",
                    )
                elif len(new_dimensions) < preferred_new_values:
                    issues.append(
                        "depth_expansion_minimum_only:"
                        f"new_values={len(new_dimensions)}, preferred={preferred_new_values}"
                    )
            elif normalized_depth_expansion.status == DEPTH_EXPANSION_INSUFFICIENT_DISTINCT_VALUE:
                if len(new_dimensions) >= minimum_new_values:
                    _issue(
                        issues, codes, details, affected,
                        "depth_expansion_status_conflict",
                        "已声明足够不同购买价值，不应仍报深度不足。",
                    )
                if normalized_duration_plan.duration_status != DURATION_STATUS_INSUFFICIENT_FOR_TARGET:
                    _issue(
                        issues, codes, details, affected,
                        "depth_expansion_duration_status_conflict",
                        "深度不足时 duration_plan 必须标为 insufficient_for_target。",
                    )
    if duration_plan_needs_refinement(
        replace(plan, duration_plan=normalized_duration_plan),
        actual_duration=total,
    ):
        measured_assessment["duration_budget_status"] = "director_budget_unfulfilled"
        measured_assessment["duration_refinement_recommended"] = True
        issues.append(
            "duration_budget_unfulfilled:"
            f"actual={total:.1f}s,director_feasible="
            f"{normalized_duration_plan.feasible_min_seconds:.1f}-{normalized_duration_plan.feasible_max_seconds:.1f}s"
        )
    if status == PLAN_STATUS_INSUFFICIENT_MATERIAL:
        issues.append(f"total_{total}s_below_{plan.target_duration}s")
        reported_status = str((plan.duration_assessment or {}).get("status") or "").strip().lower()
        if reported_status != PLAN_STATUS_INSUFFICIENT_MATERIAL:
            issues.append(
                f"duration_assessment_corrected_by_measurement:actual={total:.1f}s, "
                f"preferred_low={preferred_low:.1f}s"
            )
    elif status == PLAN_STATUS_ACCEPTABLE_LONG:
        issues.append(
            f"duration_above_preferred_range:actual={total:.1f}s, preferred_high={preferred_high:.1f}s"
        )

    replan = None
    if codes:
        replan = ReplanRequest(tuple(codes), tuple(details), tuple(affected))
    return NarrativePlan(
        strategy_id=plan.strategy_id,
        thesis=plan.thesis,
        target_duration=plan.target_duration,
        beats=plan.beats,
        status=status,
        recommended_duration=total,
        issues=tuple(issues),
        removed_beats=(),
        plan_valid=not codes,
        story_brief=plan.story_brief,
        opening_package=plan.opening_package,
        selection_contract=plan.selection_contract,
        selected_candidates=selected_candidates,
        replan_request=replan,
        duration_assessment=measured_assessment,
        duration_plan=normalized_duration_plan,
        depth_expansion=normalized_depth_expansion,
        duration_expansion_scout=plan.duration_expansion_scout,
        story_consumption=plan.story_consumption,
        opening_quality_review=plan.opening_quality_review,
    )


def plan_narrative_llm(
    *,
    strategy: Strategy,
    target_duration: float = 45.0,
    safe_candidates: Sequence[PlanningCandidate] = (),
    selection_contract: Mapping[str, Any] | None = None,
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
    api_key: str,
    base_url: str,
    model: str,
    log_fn: Callable[[str], None] | None = None,
    raw_response_hook: Callable[[str], None] | None = None,
) -> NarrativePlan:
    """Plan chapters against the full frozen safe pool, then audit without repair."""

    if not safe_candidates:
        raise ValueError("M2 requires full hard-safe candidates; do not plan from M1 evidence alone")
    brief = CommercialStoryBrief.from_strategy(strategy)
    annotated_candidates = bind_story_assets(brief, tuple(safe_candidates))
    prompt = build_planner_prompt(
        strategy, target_duration, annotated_candidates, selection_contract, executable_evidence,
    )
    raw = _post_planner_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        user_prompt=prompt,
        stage="M2_story_planner",
    )
    if raw_response_hook:
        raw_response_hook(raw)
    data = _extract_json(raw)
    plan = NarrativePlan(
        strategy_id=strategy.strategy_id,
        thesis=strategy.thesis,
        target_duration=float(target_duration),
        beats=_parse_beats(data),
        status=PLAN_STATUS_INSUFFICIENT_MATERIAL,
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=brief,
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=dict(selection_contract or {}),
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=float(target_duration)),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=float(target_duration)),
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    validated = validate_narrative_plan(
        plan, annotated_candidates, executable_evidence=executable_evidence,
    )
    if log_fn:
        log_fn(
            f"Commercial Story Planner: {strategy.strategy_id} chapters={len(validated.beats)} "
            f"actual={validated.total_seconds}s status={validated.status} valid={validated.plan_valid}"
        )
    return validated


def _build_opening_evidence_cards(
    brief: CommercialStoryBrief,
    candidates: Sequence[PlanningCandidate],
    evidence_view: Sequence[ExecutableEvidence],
    *,
    frozen_tail_ids: set[int],
) -> list[dict[str, Any]]:
    """Build source-grounded cards for the localized Opening director.

    These cards are an auditable *view* over the Commercial Asset Ledger, M1
    evidence and M3 materialization facts.  They are deliberately not a new
    candidate filter or a locally-ranked Hook whitelist: every current-Hero,
    materializable and tail-unoccupied evidence candidate is shown exactly
    once, with the roles that caused it to be attached to the story.
    """

    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    annotations: dict[int, list[dict[str, str]]] = {}
    for item in evidence_view:
        if not item.materializable or item.candidate_id in frozen_tail_ids:
            continue
        candidate = by_id.get(item.candidate_id)
        if candidate is None:
            continue
        annotations.setdefault(item.candidate_id, []).append({
            "commercial_role": item.commercial_role,
            "story_tier": item.story_tier,
        })

    cards: list[dict[str, Any]] = []
    for candidate in candidates:
        roles = annotations.get(candidate.candidate_id)
        if not roles:
            continue
        cards.append({
            "candidate_id": candidate.candidate_id,
            "text": candidate.text,
            "duration_seconds": candidate.duration,
            "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            "commercial_asset_roles": roles,
            "candidate_asset_tiers": list(candidate.asset_tiers),
            "subject_relation": candidate.subject_relation,
            "role_permissions": list(candidate.role_permissions),
            "materializable": True,
            "lineage": "verified_candidate_to_semantic_srt_to_word_span",
        })
    return cards


def build_opening_quality_review_prompt(
    strategy: Strategy,
    plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
) -> str:
    """Build the focused, source-only M2 Opening Replan v2 request."""

    brief = CommercialStoryBrief.from_strategy(strategy)
    candidates = bind_story_assets(brief, tuple(safe_candidates))
    opening = plan.opening_package
    current_opening = {
        "opening_package": opening.to_dict() if opening else None,
        "chapters": [beat.to_dict() for beat in plan.beats[:2]],
        "opening_unit_seconds": _opening_unit_seconds(opening, candidates),
        "preferred_opening_unit_max_seconds": OPENING_UNIT_PREFERRED_MAX_SECONDS,
        "warning_seconds": OPENING_UNIT_WARNING_SECONDS,
        "human_review_seconds": OPENING_UNIT_HUMAN_REVIEW_SECONDS,
        "actual_opening_text": [
            {
                "chapter_id": beat.chapter_id,
                "candidate_ids": list(beat.candidate_ids),
                "text": [
                    candidate.text for candidate in candidates if candidate.candidate_id in beat.candidate_ids
                ],
            }
            for beat in plan.beats[:2]
        ],
    }
    frozen_tail = [beat.to_dict() for beat in plan.beats[2:]]
    evidence_view = build_executable_evidence_view(brief, candidates, executable_evidence)
    hero_opening_ids = {
        item.candidate_id for item in evidence_view
        if item.materializable and item.candidate_id in {candidate.candidate_id for candidate in candidates}
    }
    frozen_tail_ids = {
        candidate_id for beat in plan.beats[2:] for candidate_id in beat.candidate_ids
    }
    hero_opening_candidates = tuple(
        candidate for candidate in candidates
        if candidate.candidate_id in hero_opening_ids and candidate.candidate_id not in frozen_tail_ids
    )
    opening_evidence_cards = _build_opening_evidence_cards(
        brief, candidates, evidence_view, frozen_tail_ids=frozen_tail_ids,
    )
    return "\n".join((
        "你现在是同一位 M2 Commercial Story Planner 的 Opening Replan v2 导演。",
        "这不是整条视频重规划：M1 Hero、商业方向、C3 及之后所有章节、顺序和候选均已冻结。"
        "你只可复核并在必要时重导 C1 Hook 与 C2 immediate payoff。",
        "目标不是压缩旧 C1/C2，而是在当前 Hero 的全部可物化证据中重新导演最强、最独立的 Opening。"
        "不要默认沿用旧候选；也不要为了变短把完整承诺削成依赖前文的表达。",
        "用真实候选原文做商业语义判断；不要建立关键词黑名单，也不要把时长数字当自动结论。",
        "选择优先级固定为：商业承诺强度 → 独立成立 → Hook 到 Payoff 的真实兑现 → 紧凑度。",
        "商业判断对照（是导演判断原则，不是字符串规则）：具体结果、消费者利益、矛盾或变化，通常强于"
        "泛化商品评价；一句脱离直播上下文仍能让观众知道为什么继续看，通常强于普通商品介绍；"
        "Payoff 应证明或推进 Hook 的承诺，而不是换个说法再说一次。",
        "例如，'肩线往里收，肩宽视觉立刻窄一截'提供可继续观看的结果；'这个版型很有风格'若没有"
        "具体利益或后续兑现，通常只是描述。示例只说明判断尺度，绝不允许改写或拼造候选原文。",
        "必须逐项判断：Hook 是否脱离直播上下文独立成立；是否含明显直播过程话；承诺是否足够快出现；"
        "Payoff 是否真正推进而非重复 Hook；Opening 是否紧凑。hook_integrity_reason 只是一条待核对的解释，"
        "不能自证通过。超过12秒只触发本次质量复核，不自动等于非法。",
        "你的判断是供人工 Opening Gate 复核的导演证据，不是自动商业通过。若当前证据没有更强的独立开场，"
        "必须诚实返回 opening_replan_failed_no_better_opening，不能为了交付而选泛化或依赖前文的句子。",
        "Commercial Story Brief:",
        json.dumps(brief.payload(), ensure_ascii=False, indent=2),
        "Executable Evidence View（false 仅说明不可物化，绝不可选择）：",
        json.dumps([item.to_dict() for item in evidence_view], ensure_ascii=False, indent=2),
        "当前 Opening（真实文本和时间）：",
        json.dumps(current_opening, ensure_ascii=False, indent=2),
        "冻结的后续章节（不得输出、修改或重新排序）：",
        json.dumps(frozen_tail, ensure_ascii=False, indent=2),
        "当前 Hero 的可物化且未被冻结后续章节占用的 Opening Evidence（仅可从此处为 C1/C2 选择；"
        "它们不是原 Opening 的白名单）：",
        _format_candidate_pool(hero_opening_candidates) or "  - [EMPTY]",
        "Opening Evidence Cards（候选级商业资产事实；全部可提交给 M3，不是本地排序或候选白名单）：",
        json.dumps(opening_evidence_cards, ensure_ascii=False, indent=2),
        "C3+ 已占用的候选不能复用于 C1/C2；后续章节冻结不等于允许 Opening 抢用其证据。",
        "返回 JSON。quality_status 只能是 pass、replan 或 opening_replan_failed_no_better_opening。"
        "若 pass，当前 Opening 已满足质量；若 opening_replan_failed_no_better_opening，说明在当前 Hero Evidence"
        "中没有更强且独立的 Opening，保留原 Opening 并具体说明缺少什么。两者均不输出 opening_package/opening_chapters。"
        "若 replan，必须仅输出新的 opening_package 与恰好两个 opening_chapters（C1、C2）；"
        "它们必须用真实 candidate_id，C1/C2 的 ID 必须分别与 opening_package 的 hook/payoff ID 完全一致。"
        "quality_status=replan 必须实际替换至少一个 Hook 或 Payoff candidate_id；原样返回同一组 ID 不是重导。"
        "不要输出 C3 之后章节，不要改 Hero、story_consumption、时长合同或任何其它故事结构。",
        '{"contract_version":"m2-opening-replan-v2", "quality_status":"pass" | "replan" | "opening_replan_failed_no_better_opening", "reason":"具体质量判断", '
        '"issues":["具体语义问题"], "hook_independence":"判断", '
        '"live_process_talk":"判断", "promise_speed":"判断", '
        '"payoff_relation":"判断", "compactness":"判断", '
        '"opening_decision_audit":{"original_opening_failure_reason":"...","candidate_card_ids_considered":[1,2],'
        '"new_hook_standalone_text":"只能引用所选候选原文","new_payoff_standalone_text":"只能引用所选候选原文",'
        '"why_hook_worth_staying":"具体消费者利益/矛盾/变化","how_payoff_delivers":"如何立即证明或推进",'
        '"commercial_gate_recommendation":"human_review_required"}, '
        '"opening_package":{"hook_promise":"...","payoff_delivery":"...",'
        '"connection_reason":"...","hook_integrity_reason":"...",'
        '"hook_candidate_ids":[1],"payoff_candidate_ids":[2],"selection_instruction":"..."}, '
        '"opening_chapters":[{"chapter_id":"C1","narrative_role":"hook","candidate_ids":[1],'
        '"goal":"...","required":true,"selection_instruction":"...","story_support":"..."},'
        '{"chapter_id":"C2","narrative_role":"development","candidate_ids":[2],'
        '"goal":"...","required":true,"selection_instruction":"...",'
        '"transition_from_previous":"...","story_support":"..."}]}',
    ))


def review_opening_quality_llm(
    *,
    strategy: Strategy,
    plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
    api_key: str,
    base_url: str,
    model: str,
    log_fn: Callable[[str], None] | None = None,
    raw_response_hook: Callable[[str], None] | None = None,
) -> NarrativePlan:
    """Perform one M2-only semantic review/replan of the opening unit.

    The program never chooses a replacement.  If the model elects a replan,
    only its returned C1/C2 replace the opening; the old tail must compare
    byte-for-byte as the same ``NarrativeBeat`` objects before validation.
    """

    if not plan.plan_valid or len(plan.beats) < 2:
        return replace(plan, opening_quality_review=OpeningQualityReview(
            status="unreported", reason="opening review requires an already valid plan with C1/C2",
            issues=("opening_review_precondition_failed",), hook_independence="", live_process_talk="",
            promise_speed="", payoff_relation="", compactness="", opening_unit_seconds=0.0,
        ))
    brief = CommercialStoryBrief.from_strategy(strategy)
    annotated = bind_story_assets(brief, tuple(safe_candidates))
    hero_evidence_ids = {
        item.candidate_id
        for item in build_executable_evidence_view(brief, annotated, executable_evidence)
        if item.materializable
    }
    frozen_tail_ids = {
        candidate_id for beat in plan.beats[2:] for candidate_id in beat.candidate_ids
    }
    raw = _post_planner_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        user_prompt=build_opening_quality_review_prompt(strategy, plan, annotated, executable_evidence),
        max_tokens=1400,
        stage="M2_opening_replan_v2",
    )
    if raw_response_hook:
        raw_response_hook(raw)
    data = _extract_json(raw)
    status = str(data.get("quality_status") or data.get("status") or "").strip().lower()
    if status != "replan":
        review = _parse_opening_quality_review(
            data, opening_unit_seconds=_opening_unit_seconds(plan.opening_package, annotated),
        )
        return replace(plan, opening_quality_review=review)

    opening = _parse_opening_package(data.get("opening_package"))
    opening_beats = _parse_beats({"chapters": data.get("opening_chapters")})
    if opening is None or len(opening_beats) != 2:
        review = _parse_opening_quality_review(
            {**data, "quality_status": "invalid_replan", "issues": ["opening_replan_shape_invalid"]},
            opening_unit_seconds=_opening_unit_seconds(plan.opening_package, annotated),
        )
        return replace(plan, opening_quality_review=review)
    expected_ids = (plan.beats[0].chapter_id, plan.beats[1].chapter_id)
    if tuple(beat.chapter_id for beat in opening_beats) != expected_ids:
        review = _parse_opening_quality_review(
            {**data, "quality_status": "invalid_replan", "issues": ["opening_replan_chapter_identity_changed"]},
            opening_unit_seconds=_opening_unit_seconds(opening, annotated),
        )
        return replace(plan, opening_quality_review=review)
    opening_ids = set((*opening.hook_candidate_ids, *opening.payoff_candidate_ids))
    if not opening_ids.issubset(hero_evidence_ids):
        review = _parse_opening_quality_review(
            {**data, "quality_status": "invalid_replan", "issues": ["opening_replan_outside_hero_executable_evidence"]},
            opening_unit_seconds=_opening_unit_seconds(opening, annotated),
        )
        return replace(plan, opening_quality_review=review)
    reused_tail_ids = sorted(opening_ids.intersection(frozen_tail_ids))
    if reused_tail_ids:
        review = _parse_opening_quality_review(
            {
                **data,
                "quality_status": "invalid_replan",
                "issues": ["opening_replan_reuses_frozen_tail_candidate:" + ",".join(map(str, reused_tail_ids))],
            },
            opening_unit_seconds=_opening_unit_seconds(opening, annotated),
        )
        return replace(plan, opening_quality_review=review)
    original_opening = plan.opening_package
    if original_opening and (
        tuple(opening.hook_candidate_ids) == tuple(original_opening.hook_candidate_ids)
        and tuple(opening.payoff_candidate_ids) == tuple(original_opening.payoff_candidate_ids)
    ):
        review = _parse_opening_quality_review(
            {**data, "quality_status": "invalid_replan", "issues": ["opening_replan_no_candidate_change"]},
            opening_unit_seconds=_opening_unit_seconds(opening, annotated),
        )
        return replace(plan, opening_quality_review=review)
    opening = _regenerate_opening_metadata(opening, annotated)
    review = _parse_opening_quality_review(
        data, opening_unit_seconds=_opening_unit_seconds(opening, annotated), replanned=True,
    )
    localized = replace(
        plan,
        beats=opening_beats + plan.beats[2:],
        opening_package=opening,
        opening_quality_review=review,
    )
    validated = validate_narrative_plan(
        localized, annotated, executable_evidence=executable_evidence,
    )
    if log_fn:
        log_fn(
            f"Opening Quality Review: {strategy.strategy_id} status={review.status} "
            f"replanned={review.replanned} valid={validated.plan_valid}"
        )
    return validated


def build_replan_prompt(
    strategy: Strategy,
    target_duration: float,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    invalid_plan: NarrativePlan,
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
) -> str:
    """Ask the director to correct its own plan; no local story patching occurs."""

    measured_by_id = {
        candidate.candidate_id: candidate.duration
        for candidate in invalid_plan.selected_candidates
    }
    chapter_measurements = [
        {
            "chapter_id": beat.chapter_id,
            "candidate_ids": list(beat.candidate_ids),
            "actual_seconds": round(sum(measured_by_id.get(candidate_id, 0.0) for candidate_id in beat.candidate_ids), 3),
        }
        for beat in invalid_plan.beats
    ]
    preferred_low, preferred_high = _duration_preferred_range(target_duration)
    actual = invalid_plan.total_seconds
    duration_gap: dict[str, float] = {
        "preferred_low": preferred_low,
        "preferred_high": preferred_high,
        "actual_seconds": actual,
    }
    if actual < preferred_low:
        duration_gap["minimum_seconds_to_add"] = round(preferred_low - actual, 3)
    elif actual > preferred_high:
        duration_gap["minimum_seconds_to_remove"] = round(actual - preferred_high, 3)
    previous = {
        "opening_package": invalid_plan.opening_package.to_dict() if invalid_plan.opening_package else None,
        "chapters": [beat.to_dict() for beat in invalid_plan.beats],
        "chapter_actual_seconds": chapter_measurements,
        "actual_seconds": invalid_plan.total_seconds,
        "measured_duration_gap": duration_gap,
        "issues": list(invalid_plan.issues),
        "replan_request": invalid_plan.replan_request.to_dict() if invalid_plan.replan_request else None,
        "duration_assessment": dict(invalid_plan.duration_assessment or {}),
        "depth_expansion": invalid_plan.depth_expansion.to_dict() if invalid_plan.depth_expansion else None,
    }
    opening_measurement: dict[str, Any] = {}
    if invalid_plan.opening_package is not None:
        safe_by_id = {candidate.candidate_id: candidate for candidate in safe_candidates}
        hook_ids = list(invalid_plan.opening_package.hook_candidate_ids)
        hook_seconds = round(sum(
            safe_by_id[candidate_id].duration
            for candidate_id in hook_ids
            if candidate_id in safe_by_id
        ), 3)
        opening_measurement = {
            "hook_candidate_ids": hook_ids,
            "actual_hook_seconds": hook_seconds,
            "preferred_hook_range_seconds": [HOOK_PREFERRED_MIN_SECONDS, HOOK_PREFERRED_MAX_SECONDS],
            "acceptable_hook_max_seconds": HOOK_ACCEPTABLE_MAX_SECONDS,
            "rejected_as_hook_group": hook_seconds > HOOK_ACCEPTABLE_MAX_SECONDS,
        }
    return "\n".join((
        build_planner_prompt(
            strategy, target_duration, safe_candidates, selection_contract, executable_evidence,
        ),
        "",
        "上一次导演方案未通过程序审计。请重新输出完整 JSON 方案，不要解释，也不要让程序替你修。",
        "必须逐项解决以下确定性问题：",
        json.dumps(previous, ensure_ascii=False, indent=2),
        "Opening Contract 的程序实测（不是建议；rejected_as_hook_group=true 时，这组 ID 不能原样再次作为 Hook）：",
        json.dumps(opening_measurement, ensure_ascii=False, indent=2),
        "上面的 actual_seconds、chapter_actual_seconds 和 measured_duration_gap 由程序按真实候选时间戳计算，"
        "它们是唯一可信的时长事实；不得用自己的估算覆盖它们。",
        "修正规则：",
        "- opening_package 必须提交 hook_promise、payoff_delivery、connection_reason；C1 candidate_ids 必须与 "
        "hook_candidate_ids 完全一致，C2 candidate_ids 必须与 payoff_candidate_ids 完全一致。",
        "- 若使用 Bridge，story_consumption.bridge_candidate_ids 必须与最终 chapters 实际选中的 bridge tier "
        "candidate_id 完全一致；不能只声明不消费。",
        "- Hook 优选 2.5-5 秒；5-8 秒仅在语义完整、没有直播废话且不能不损失承诺地再压缩时允许，"
        "此时必须填写 hook_integrity_reason。超过 8 秒才是不可提交的超长 Hook。",
        "- 若 Opening Contract 实测为 rejected_as_hook_group=true，必须改用另一组合法 Hook candidate_id，"
        "或只保留该组中的合法子集；不得原样返回被拒绝的完整 ID 组。Payoff 和后续章节仍由你重新导演。",
        "- 对时长问题只能二选一：A. 从完整安全池补入仍服务主故事的新章节，使真实总时长进入优先区间，"
        "此时 status=full；B. 若相关资产确实不足，保留最完整的短版，但 status 必须为 "
        "insufficient_material，并具体说明缺少哪种资产。短于优先下限时严禁写 full。",
        "- 若实际时长超过优先区间，必须删除整个重复或不推进的章节，或改选更短的完整候选，"
        "使真实总时长进入优先区间；不能截断文本，也不能因为每段都不错就全部保留。",
        "- 保留有价值的故事意图即可，候选、章节、顺序均可由你重新导演。",
    ))


def build_duration_refinement_prompt(
    strategy: Strategy,
    current_plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    duration_expansion_scout: DurationExpansionScout | None = None,
) -> str:
    """Ask the director to reconcile a legal plan with its declared duration budget."""

    target = current_plan.target_duration
    preferred_low, preferred_high = _duration_preferred_range(target)
    depth_expansion = current_plan.depth_expansion or _fallback_depth_expansion(
        target,
        reason="当前方案没有可用的长版深度扩展记录。",
    )
    covered_values = set(depth_expansion.base_covered_values)
    covered_values.update(value.dimension for value in depth_expansion.new_value_chapters)
    selected_candidate_ids = {
        candidate_id
        for beat in current_plan.beats
        for candidate_id in beat.candidate_ids
    }
    shortfall_seconds = round(max(0.0, preferred_low - current_plan.total_seconds), 3)
    remaining_safe_count = sum(
        1 for candidate in safe_candidates
        if candidate.candidate_id not in selected_candidate_ids
    )
    uncovered_value_dimensions = sorted(DEPTH_NEW_VALUE_DIMENSIONS - covered_values)
    remaining_catalog = _format_duration_expansion_catalog(
        safe_candidates,
        selected_candidate_ids=selected_candidate_ids,
    )
    duration_budget_options = _duration_expansion_budget_options(
        duration_expansion_scout,
        additional_seconds_needed=shortfall_seconds,
    )
    current = {
        "opening_package": current_plan.opening_package.to_dict() if current_plan.opening_package else None,
        "duration_plan": current_plan.duration_plan.to_dict() if current_plan.duration_plan else None,
        "duration_assessment": dict(current_plan.duration_assessment or {}),
        "depth_expansion": current_plan.depth_expansion.to_dict() if current_plan.depth_expansion else None,
        "actual_planned_duration": current_plan.total_seconds,
        "preferred_duration_range": {
            "min_seconds": preferred_low,
            "max_seconds": preferred_high,
        },
        "duration_expansion_audit": {
            "additional_seconds_needed": shortfall_seconds,
            "covered_purchase_values": sorted(covered_values),
            "uncovered_value_dimensions_to_search_first": uncovered_value_dimensions,
            "currently_selected_candidate_count": len(selected_candidate_ids),
            "remaining_safe_candidate_count": remaining_safe_count,
        },
        "duration_expansion_scout": (
            duration_expansion_scout.to_dict() if duration_expansion_scout else None
        ),
        "duration_expansion_budget_options": list(duration_budget_options),
        "chapters": [beat.to_dict() for beat in current_plan.beats],
    }
    long_form_required, _long_form_preferred = _depth_expansion_requirement(target)
    if shortfall_seconds > 0.1 and long_form_required:
        shortfall_instruction = (
            f"这是一轮长版深度扩展：还需要约 {shortfall_seconds:.1f}s 的真实完整口播。先从尚未消费的购买价值"
            f"中找：{', '.join(uncovered_value_dimensions) or '无'}。新增内容必须是主故事的自然延展，例如新人群、"
            "新场景、舒适度、搭配结果、新顾虑、信任、使用周期或情绪价值；不要再补同一机制、同一显瘦结果的"
            "换句话证明。若新增章节，必须同时写入 chapters.value_dimension 和 depth_expansion.new_value_chapters。"
            "只有完整安全池确实没有这些不同价值时，才可报 insufficient_for_target / insufficient_distinct_value。"
        )
    elif shortfall_seconds > 0.1:
        shortfall_instruction = (
            f"这是一轮时长补足：还需要约 {shortfall_seconds:.1f}s 的真实完整口播。请重新搜索未使用的安全候选，"
            "只补能推进当前商业故事的新信息、场景、信任或结果；不能用同义句和重复证明凑时长。"
        )
    else:
        shortfall_instruction = (
            "当前计划不短于优先下限；只在确有重复或不推进内容时压缩，不能为了形式重排故事。"
        )
    return "\n".join((
        build_planner_prompt(strategy, target, safe_candidates, selection_contract),
        "",
        "上一次方案的候选、边界、Hook 和顺序都已通过结构审计，但它的真实时长与自己声明的叙事预算不一致。"
        "请作为导演重新输出完整 JSON，不要解释，也不要让程序替你删片。",
        json.dumps(current, ensure_ascii=False, indent=2),
        "M2 Duration Expansion Scout（仅帮助你重新发现未使用资产，不是白名单、不是章节顺序、也不替你选片）：",
        json.dumps(
            duration_expansion_scout.to_dict() if duration_expansion_scout else {
                "status": EXPANSION_SCOUT_UNREPORTED,
                "reason": "本次没有单独的长版资产勘查结果。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        "Duration budget witnesses（仅是已勘查资产的数学组合，不是推荐片单：只有你确认它们仍是同主商品、"
        "符合合同并能自然服务故事时才能使用；若要报 feasible，必须选用其中一个组合或等价的真实时长组合）：",
        json.dumps(list(duration_budget_options), ensure_ascii=False, indent=2),
        "未使用完整安全候选速查表（不是白名单；本轮短缺时必须实际检查这里是否存在可推进主故事的新价值）：",
        remaining_catalog,
        "本次任务只做导演级时长重规划：保留 Commercial Story 的核心承诺和购买推进，不要为了长度把故事压回"
        "单一卖点论证。若实际过长，优先移除重复、没有新增购买价值的完整章节，或选择更短但完整的等价表达；"
        "若实际过短，只有在完整安全池中存在仍服务主故事的新证据、场景、信任或拥有感时才可补入，"
        "否则必须诚实改报 duration_status=insufficient_for_target。不得截断候选、不得添加无关卖点、"
        "不得让程序自行补片。",
        f"当前真实时长为 {current_plan.total_seconds:.1f}s，优先区间为 {preferred_low:.1f}-{preferred_high:.1f}s。"
        "你上次声明的 feasible_duration_range 也在上方；不得再把候选表中不存在的时长写进报告。"
        "若完整安全池中不存在更自然的长度，诚实返回 duration_status=insufficient_for_target，"
        "并让 feasible_duration_range / recommended_duration 反映最佳商业长度。",
        shortfall_instruction,
        (
            f"本轮候选真实缺口是 {shortfall_seconds:.1f}s。若 Scout.status=found，必须从其中或完整安全池中实际选入"
            "足以覆盖该缺口的同主商品、新购买价值候选组合，并用候选表 duration 重算总秒数；不能只挑一两句后仍把"
            "计划报为 feasible。若所有同主商品的合法组合也覆盖不了缺口，才可报 insufficient_for_target。"
            if shortfall_seconds > 0.1
            else "本轮没有短于优先下限；不要为了使用 Scout 资产而额外加片。"
        ),
        "主商品一致性自检：Scout 和完整安全池中可能有搭配或同场其他单品的表达；只要该候选的主体已经转成"
        "裤子、其他衣服或非本次主商品，就不能作为主故事的新章节。可以讲搭配结果，但不能把别的商品介绍冒充本品价值。",
        "禁止原样返回同一组 candidate_id 后仍声明 feasible 或 full。若没有实际补入能推进故事的完整候选，"
        "必须把 duration_plan.duration_status 改为 insufficient_for_target，并明确记录素材或购买价值不足。",
        "这不是让你机械删到上限以内：只有内容重复或不推进时才压缩；若计划本已自然且只是略长，"
        "可以保留完整故事并在 duration_plan 中说明原因。",
    ))


def _fallback_duration_expansion_scout(
    reason: str,
    *,
    reported: bool = False,
    scout_report: Mapping[str, Any] | None = None,
) -> DurationExpansionScout:
    return DurationExpansionScout(
        assets=(),
        status=EXPANSION_SCOUT_UNREPORTED if not reported else EXPANSION_SCOUT_INSUFFICIENT,
        reason=reason,
        reported=reported,
        scout_report=scout_report,
    )


def _parse_duration_expansion_scout(
    raw: Any,
    *,
    safe_candidates: Sequence[PlanningCandidate],
    selected_candidate_ids: Sequence[int],
) -> DurationExpansionScout:
    """Keep only real, unused candidates from an LLM duration-asset scan."""

    if not isinstance(raw, Mapping):
        return _fallback_duration_expansion_scout("长版资产勘查未返回 JSON。")
    original = dict(raw)
    candidates_by_id = {candidate.candidate_id: candidate for candidate in safe_candidates}
    safe_ids = set(candidates_by_id)
    selected_ids = {int(candidate_id) for candidate_id in selected_candidate_ids}
    assets: list[DurationExpansionAsset] = []
    for item in raw.get("expansion_assets") or ():
        if not isinstance(item, Mapping):
            continue
        try:
            candidate_id = int(item.get("candidate_id"))
        except (TypeError, ValueError):
            continue
        dimension = str(item.get("value_dimension") or "").strip().lower()
        purchase_value = str(item.get("purchase_value") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if (
            candidate_id not in safe_ids
            or candidate_id in selected_ids
            or dimension not in DEPTH_NEW_VALUE_DIMENSIONS
            or not purchase_value
            or not reason
            or any(asset.candidate_id == candidate_id for asset in assets)
        ):
            continue
        candidate = candidates_by_id[candidate_id]
        assets.append(DurationExpansionAsset(
            candidate_id,
            dimension,
            candidate.duration,
            purchase_value,
            reason,
        ))
    status = str(raw.get("status") or "").strip().lower()
    reason = str(raw.get("reason") or "").strip()
    if assets:
        return DurationExpansionScout(
            assets=tuple(assets),
            status=EXPANSION_SCOUT_FOUND,
            reason=reason or "已发现可用于长版的新购买价值资产。",
            reported=True,
            scout_report=original,
        )
    return DurationExpansionScout(
        assets=(),
        status=EXPANSION_SCOUT_INSUFFICIENT,
        reason=reason or (
            "完整安全候选中未发现能在不重复核心 claim 的前提下扩展主故事的完整资产。"
        ),
        reported=status in {EXPANSION_SCOUT_FOUND, EXPANSION_SCOUT_INSUFFICIENT},
        scout_report=original,
    )


def _duration_expansion_budget_options(
    scout: DurationExpansionScout | None,
    *,
    additional_seconds_needed: float,
    limit: int = 8,
) -> tuple[dict[str, Any], ...]:
    """Expose duration arithmetic without selecting a narrative path.

    The options only combine assets an LLM scout already surfaced. They do not
    choose a final option, create a chapter, reorder content, or weaken the
    product and content contracts.
    """

    if not scout or scout.status != EXPANSION_SCOUT_FOUND or additional_seconds_needed <= 0.1:
        return ()
    assets = tuple(scout.assets)
    required_dimensions = 2 if len({asset.value_dimension for asset in assets}) >= 2 else 1
    options: list[tuple[float, int, tuple[int, ...], tuple[DurationExpansionAsset, ...]]] = []
    for size in range(1, min(5, len(assets)) + 1):
        for combo in combinations(assets, size):
            seconds = round(sum(asset.candidate_duration for asset in combo), 3)
            dimensions = {asset.value_dimension for asset in combo}
            if seconds + 0.001 < additional_seconds_needed or len(dimensions) < required_dimensions:
                continue
            options.append((
                round(seconds - additional_seconds_needed, 3),
                -len(dimensions),
                tuple(asset.candidate_id for asset in combo),
                combo,
            ))
    rendered: list[dict[str, Any]] = []
    for _excess, _dimension_count, _ids, combo in sorted(options)[:max(1, int(limit))]:
        rendered.append({
            "candidate_ids": [asset.candidate_id for asset in combo],
            "additional_seconds": round(sum(asset.candidate_duration for asset in combo), 3),
            "value_dimensions": sorted({asset.value_dimension for asset in combo}),
        })
    return tuple(rendered)


def build_duration_expansion_scout_prompt(
    strategy: Strategy,
    current_plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
) -> str:
    """Ask M2 to recall unused long-form assets before it re-directs a plan."""

    target = max(1.0, float(current_plan.target_duration or 1.0))
    preferred_low, preferred_high = _duration_preferred_range(target)
    required, _preferred = _depth_expansion_requirement(target)
    if not required:
        raise ValueError("duration expansion scout is only valid for long-form planning")
    selected_ids = tuple(
        candidate_id
        for beat in current_plan.beats
        for candidate_id in beat.candidate_ids
    )
    shortfall = round(max(0.0, preferred_low - current_plan.total_seconds), 3)
    depth = current_plan.depth_expansion or _fallback_depth_expansion(
        target,
        reason="当前方案没有可用的深度扩展合同。",
    )
    covered_values = set(depth.base_covered_values)
    covered_values.update(value.dimension for value in depth.new_value_chapters)
    uncovered = sorted(DEPTH_NEW_VALUE_DIMENSIONS - covered_values)
    brief = CommercialStoryBrief.from_strategy(strategy)
    return "\n".join((
        "M2 Duration Expansion Scout：你是商业故事的资产勘查员，不是剪辑师。",
        "不要输出片单、章节、顺序、Hook 或改写字幕。你的唯一任务是在未使用的完整 hard-safe 候选中，"
        "找出能让同一个 Commercial Story 变深的新购买价值资产，交给下一位导演选择。",
        "Commercial Story Brief:",
        json.dumps(brief.payload(), ensure_ascii=False, indent=2),
        "Selection Contract:",
        json.dumps(dict(selection_contract or {}), ensure_ascii=False, sort_keys=True),
        "当前已批准但过短的计划审计:",
        json.dumps({
            "target_duration": target,
            "actual_planned_duration": current_plan.total_seconds,
            "preferred_duration_range": {"min_seconds": preferred_low, "max_seconds": preferred_high},
            "additional_seconds_needed": shortfall,
            "selected_candidate_ids": list(selected_ids),
            "covered_purchase_values": sorted(covered_values),
            "uncovered_value_dimensions_to_search_first": uncovered,
            "current_chapters": [beat.to_dict() for beat in current_plan.beats],
        }, ensure_ascii=False, indent=2),
        "勘查标准：候选必须仍指向同一主商品和主故事，且必须带来新的人群、场景、舒适度、搭配结果、"
        "购买顾虑解决、信任、使用周期或情绪价值。不要把同一个显瘦机制、同一个结果、同义重复证明写进来。"
        "价格/CTA/尺码互动等仍受合同约束，不能因为可延长时长而放行。",
        "未使用完整 hard-safe 候选（不是允许列表，必须只按真实 id/duration/text 勘查）：",
        _format_duration_expansion_catalog(safe_candidates, selected_candidate_ids=selected_ids),
        "返回 JSON：",
        "{",
        '  "status": "found" | "insufficient_distinct_value",',
        '  "reason": "本轮资产是否足以自然扩展同一个故事",',
        '  "expansion_assets": [',
        '    {"candidate_id": 101, "value_dimension": "new_scene", '
        '"purchase_value": "新增的场景购买理由", "reason": "它怎样服务核心故事"}',
        "  ]",
        "}",
        "每个 candidate_id 只能出现一次，最多返回 12 条；不要把已经选中的候选重复列回。"
        "若没有足够的不同购买价值，诚实返回空数组和 insufficient_distinct_value。",
    ))


def discover_duration_expansion_assets_llm(
    *,
    strategy: Strategy,
    current_plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    api_key: str,
    base_url: str,
    model: str,
    raw_response_hook: Callable[[str], None] | None = None,
) -> DurationExpansionScout:
    """Use one narrow M2 recall pass before a long-form replan.

    The result is deliberately advisory: it makes ignored safe assets visible
    but never changes the current plan or chooses the next plan locally.
    """

    target = max(1.0, float(current_plan.target_duration or 1.0))
    required, _preferred = _depth_expansion_requirement(target)
    preferred_low, _preferred_high = _duration_preferred_range(target)
    if not required or current_plan.total_seconds >= preferred_low:
        return _fallback_duration_expansion_scout(
            "当前时长不需要长版资产勘查。",
            reported=True,
        )
    annotated_candidates = bind_story_assets(
        CommercialStoryBrief.from_strategy(strategy),
        tuple(safe_candidates),
    )
    raw = _post_planner_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        user_prompt=build_duration_expansion_scout_prompt(
            strategy,
            current_plan,
            annotated_candidates,
            selection_contract,
        ),
        stage="M2_duration_scout",
    )
    if raw_response_hook:
        raw_response_hook(raw)
    selected_ids = tuple(
        candidate_id
        for beat in current_plan.beats
        for candidate_id in beat.candidate_ids
    )
    return _parse_duration_expansion_scout(
        _extract_json(raw),
        safe_candidates=annotated_candidates,
        selected_candidate_ids=selected_ids,
    )


def refine_duration_narrative_llm(
    *,
    strategy: Strategy,
    current_plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    api_key: str,
    base_url: str,
    model: str,
    log_fn: Callable[[str], None] | None = None,
    raw_response_hook: Callable[[str], None] | None = None,
    duration_expansion_scout_hook: Callable[[str], None] | None = None,
) -> NarrativePlan:
    """Perform one AI-authored duration refinement for a structurally legal plan.

    This is deliberately not called by the live path.  The prototype harness
    invokes it only when the plan contradicts its stated feasible range, so M3 never
    becomes a second director or a local duration patcher.
    """

    if not current_plan.plan_valid:
        return current_plan
    brief = CommercialStoryBrief.from_strategy(strategy)
    annotated_candidates = bind_story_assets(brief, tuple(safe_candidates))
    expansion_scout = discover_duration_expansion_assets_llm(
        strategy=strategy,
        current_plan=current_plan,
        safe_candidates=annotated_candidates,
        selection_contract=selection_contract,
        api_key=api_key,
        base_url=base_url,
        model=model,
        raw_response_hook=duration_expansion_scout_hook,
    )
    raw = _post_planner_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        user_prompt=build_duration_refinement_prompt(
            strategy,
            current_plan,
            annotated_candidates,
            selection_contract,
            expansion_scout,
        ),
        stage="M2_duration_replan",
    )
    if raw_response_hook:
        raw_response_hook(raw)
    data = _extract_json(raw)
    refined = NarrativePlan(
        strategy_id=strategy.strategy_id,
        thesis=strategy.thesis,
        target_duration=current_plan.target_duration,
        beats=_parse_beats(data),
        status=PLAN_STATUS_INSUFFICIENT_MATERIAL,
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=brief,
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=dict(selection_contract or {}),
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=current_plan.target_duration),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=current_plan.target_duration),
        duration_expansion_scout=expansion_scout,
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    validated = validate_narrative_plan(refined, annotated_candidates)
    if log_fn:
        log_fn(
            f"Commercial Story Planner duration refinement: {strategy.strategy_id} chapters={len(validated.beats)} "
            f"actual={validated.total_seconds}s status={validated.status} valid={validated.plan_valid}"
        )
    return validated


def replan_narrative_llm(
    *,
    strategy: Strategy,
    invalid_plan: NarrativePlan,
    safe_candidates: Sequence[PlanningCandidate],
    selection_contract: Mapping[str, Any] | None,
    executable_evidence: Mapping[int, Mapping[str, Any]] | None = None,
    api_key: str,
    base_url: str,
    model: str,
    log_fn: Callable[[str], None] | None = None,
    raw_response_hook: Callable[[str], None] | None = None,
) -> NarrativePlan:
    """Perform one AI-authored correction for a structured M2 replan request."""

    if not invalid_plan.replan_request:
        return invalid_plan
    brief = CommercialStoryBrief.from_strategy(strategy)
    annotated_candidates = bind_story_assets(brief, tuple(safe_candidates))
    raw = _post_planner_request(
        api_key=api_key,
        base_url=base_url,
        model=model,
        user_prompt=build_replan_prompt(
            strategy,
            invalid_plan.target_duration,
            annotated_candidates,
            selection_contract,
            invalid_plan,
            executable_evidence,
        ),
        stage="M2_replan",
    )
    if raw_response_hook:
        raw_response_hook(raw)
    data = _extract_json(raw)
    corrected = NarrativePlan(
        strategy_id=strategy.strategy_id,
        thesis=strategy.thesis,
        target_duration=invalid_plan.target_duration,
        beats=_parse_beats(data),
        status=PLAN_STATUS_INSUFFICIENT_MATERIAL,
        recommended_duration=0.0,
        issues=(),
        removed_beats=(),
        plan_valid=True,
        story_brief=brief,
        opening_package=_parse_opening_package(data.get("opening_package")),
        selection_contract=dict(selection_contract or {}),
        duration_assessment=_parse_duration_assessment(data.get("duration_assessment")),
        duration_plan=_parse_duration_plan(data.get("duration_plan"), target_duration=invalid_plan.target_duration),
        depth_expansion=_parse_depth_expansion(data.get("depth_expansion"), target_duration=invalid_plan.target_duration),
        story_consumption=_parse_story_consumption(data.get("story_consumption")),
    )
    validated = validate_narrative_plan(
        corrected, annotated_candidates, executable_evidence=executable_evidence,
    )
    if log_fn:
        log_fn(
            f"Commercial Story Planner replan: {strategy.strategy_id} chapters={len(validated.beats)} "
            f"actual={validated.total_seconds}s status={validated.status} valid={validated.plan_valid}"
        )
    return validated
