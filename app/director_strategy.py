"""Director Strategy Layer: turn M1's Product Story Map into sell-path options.

This layer is deliberately deterministic and source-only.  It does not invent
product claims, call a model, reorder candidates, or materialize clips.  Its
only authority is to package *already discovered* M1 stories into a small set
of named commercial intents for M2 to compose after the operator chooses one.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from commercial_analyzer import EvidenceItem, Strategy


_QUALITY_MARKERS = ("quality", "material", "fabric", "trust", "safety", "comfort", "品质", "面料", "舒适", "安全")
# Keep safety/material assurance distinct from comfort.  M1 may name the
# former ``baby_grade_*`` rather than spelling out "safety" in its angle, so
# those source-grounded material stories must remain eligible for a premium
# proposal instead of silently disappearing from the operator's choices.
_PREMIUM_MARKERS = (
    "quality", "material", "fabric", "trust", "safety", "baby", "a类",
    "母婴", "品质", "面料", "安全",
)
_UTILITY_MARKERS = ("comfort", "convenience", "lifestyle", "scene", "utility", "舒适", "搭配", "场景", "便利")
_EXCLUDED_AUTOMATIC_MARKERS = ("urgency", "scarcity", "social_proof", "促销", "预售", "稀缺")

_MODE_COPY = {
    "traffic": {
        "name": "爆款引流版",
        "icon": "🚀",
        "goal": "让目标用户先因明确痛点与可见变化停留",
        "suitable_for": "投流、新账号、冷启动内容",
        "headline": "先解决最强用户痛点，再快速展示变化",
    },
    "conversion": {
        "name": "成交转化版",
        "icon": "🛒",
        "goal": "用核心效果、适穿证明与体验价值降低购买顾虑",
        "suitable_for": "商品推广、已有兴趣人群",
        "headline": "主卖点之外，再回答“我能不能穿、穿着怎么样”",
    },
    "premium": {
        "name": "品质价值版",
        "icon": "✨",
        "goal": "把设计、材质或可信证据转成产品价值感",
        "suitable_for": "品牌表达、高客单或详情页素材",
        "headline": "先讲为什么值得买，再用真实材质与体验证据支撑",
    },
}


# P0 deliberately keeps the narrative vocabulary small.  A Blueprint is an
# ordered map of buyer-value chapters, not a script, an edit list, or a
# duration budget.  M2 may only fill a slot with source-grounded candidate
# evidence after the existing core story exists; it may also leave a slot
# empty when the full safe pool offers no clean new value.
_NARRATIVE_ARCHETYPE_TEMPLATES: dict[str, dict[str, Any]] = {
    "pain_point": {
        "name": "痛点切入型",
        "description": "先兑现最强外观痛点，再依次解除适穿、穿着与日常使用顾虑。",
        # These are audience desires and editorial constraints, not product
        # claims copied from the currently selected M1 Hero.  M2 must still
        # prove every chapter with a real candidate.
        "core_desire": "先解决大身材想显瘦、想修饰身形的购买焦虑。",
        "opening_promise": "直接给出一句干净、独立成立的显瘦或身形修饰结果。",
        "opening_scope": {
            "allowed_purchase_question_ids": ("Q1",),
            "allowed_answer_roles": ("result",),
            "fallback_to_global_opening": False,
            "requires_clean_independent_utterance": True,
        },
        "early_journey_scope": {
            "opening_question_ids": ("Q1", "Q2"),
            "required_question_ids": ("Q1", "Q2"),
            "recommended_question_ids": ("Q3", "Q5", "Q4", "Q6"),
            "optional_question_ids": ("Q7",),
            "preferred_question_order": ("Q1", "Q2", "Q3", "Q5", "Q4", "Q6", "Q7"),
        },
        "chapter_slots": (
            {"slot_id": "core_desire_result", "priority": 1, "phase": "core", "coverage": "required", "purchase_question_id": "Q1", "answer_roles": ("result",)},
            {"slot_id": "core_mechanism", "priority": 2, "phase": "core", "coverage": "required", "purchase_question_id": "Q2", "answer_roles": ("mechanism", "proof")},
            {"slot_id": "body_fit", "priority": 3, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q3", "answer_roles": ("result", "proof")},
            {"slot_id": "wearing_security", "priority": 4, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q5", "answer_roles": ("risk_remove", "proof", "comfort")},
            {"slot_id": "summer_comfort", "priority": 5, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q4", "answer_roles": ("comfort", "proof")},
            {"slot_id": "easy_styling", "priority": 6, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q6", "answer_roles": ("styling", "scene")},
            {"slot_id": "trust_close", "priority": 7, "phase": "depth", "coverage": "optional", "purchase_question_id": "Q7", "answer_roles": ("trust", "proof")},
        ),
    },
    "scene_immersion": {
        "name": "场景代入型",
        "description": "先让用户进入夏天实际出门的穿着体验，再补足适穿、效果与风险顾虑。",
        "core_desire": "先让用户想象夏天日常出门时，能不能穿得舒服、好搭、轻松。",
        "opening_promise": "先给一句干净、独立成立的夏季穿着体验或日常出门搭配感受。",
        "opening_scope": {
            "allowed_purchase_question_ids": ("Q6", "Q4"),
            "allowed_answer_roles": ("scene", "styling", "comfort", "proof"),
            "fallback_to_global_opening": False,
            "requires_clean_independent_utterance": True,
        },
        "early_journey_scope": {
            "opening_question_ids": ("Q6", "Q4"),
            "required_question_ids": ("Q6", "Q4"),
            "recommended_question_ids": ("Q1", "Q3", "Q5"),
            "optional_question_ids": ("Q2", "Q7"),
            "preferred_question_order": ("Q6", "Q4", "Q1", "Q3", "Q5", "Q2", "Q7"),
        },
        "chapter_slots": (
            {"slot_id": "scene_imagination", "priority": 1, "phase": "core", "coverage": "required", "purchase_question_id": "Q6", "answer_roles": ("scene", "styling")},
            {"slot_id": "summer_experience", "priority": 2, "phase": "core", "coverage": "required", "purchase_question_id": "Q4", "answer_roles": ("comfort", "proof")},
            {"slot_id": "visible_result", "priority": 3, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q1", "answer_roles": ("result", "proof")},
            {"slot_id": "body_fit", "priority": 4, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q3", "answer_roles": ("result", "proof")},
            {"slot_id": "wearing_security", "priority": 5, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q5", "answer_roles": ("risk_remove", "proof", "comfort")},
            {"slot_id": "mechanism_support", "priority": 6, "phase": "depth", "coverage": "optional", "purchase_question_id": "Q2", "answer_roles": ("mechanism", "proof")},
            {"slot_id": "trust_close", "priority": 7, "phase": "depth", "coverage": "optional", "purchase_question_id": "Q7", "answer_roles": ("trust", "proof")},
        ),
    },
}


def build_narrative_blueprint_contract(
    strategy: Strategy, narrative_archetype: str,
) -> dict[str, Any]:
    """Return a source-neutral Director Blueprint for the existing M2 route.

    This does not discover claims or candidates.  It only supplies the
    director's desired buyer-value order so M2 can inspect the full safe pool
    for unsupported slots when a narrative-depth target asks for it.
    """
    archetype = str(narrative_archetype or "").strip().lower()
    template = _NARRATIVE_ARCHETYPE_TEMPLATES.get(archetype)
    if template is None:
        raise ValueError(f"unsupported narrative archetype: {narrative_archetype}")
    return {
        "narrative_archetype": archetype,
        "core_desire": template["core_desire"],
        "opening_promise": template["opening_promise"],
        "opening_scope": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in template["opening_scope"].items()
        },
        "early_journey_scope": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in template["early_journey_scope"].items()
        },
        "blueprint": {
            "version": "narrative-blueprint-p0.1",
            "name": template["name"],
            "description": template["description"],
            "chapter_slots": [
                {
                    "slot_id": item["slot_id"],
                    "priority": int(item["priority"]),
                    "phase": item["phase"],
                    "coverage": item["coverage"],
                    "purchase_question_id": item["purchase_question_id"],
                    "answer_roles": list(item["answer_roles"]),
                    "duration_seconds": None,
                }
                for item in template["chapter_slots"]
            ],
            "duration_policy": "soft_target_no_padding",
            "stop_rule": "stop_when_no_unexplored_high_value_slot_has_clean_source_evidence",
        },
    }


def _text(strategy: Strategy) -> str:
    """Return M1's structured taxonomy, never its free-form sales copy.

    A word such as ``面料`` occurs in both a cool-wearing story and a material
    assurance story.  Classifying Director modes from the promise text would
    therefore make a comfort story steal the premium slot.  The story map's
    type/family/sub-angle are the available source facts for this decision.
    """
    return " ".join((
        strategy.type,
        strategy.strategy_family,
        strategy.sub_angle,
    )).lower()


def _has_marker(strategy: Strategy, markers: Sequence[str]) -> bool:
    text = _text(strategy)
    return any(marker in text for marker in markers)


def _automatic_eligible(strategy: Strategy) -> bool:
    return not _has_marker(strategy, _EXCLUDED_AUTOMATIC_MARKERS)


def _priority_key(strategy: Strategy) -> tuple[int, float, float, str]:
    priority = {"high": 0, "medium": 1, "low": 2}.get(strategy.story_priority, 3)
    return (priority, -float(strategy.story_strength or 0.0), -float(strategy.material_sufficiency or 0.0), strategy.strategy_id)


def _distinct(primary: Strategy, stories: Sequence[Strategy], markers: Sequence[str] = ()) -> Strategy | None:
    for story in stories:
        if story.strategy_id == primary.strategy_id:
            continue
        if markers and not _has_marker(story, markers):
            continue
        if story.strategy_family != primary.strategy_family or story.sub_angle != primary.sub_angle:
            return story
    return None


def _mix_item(strategy: Strategy, role: str, budget_share: float) -> dict[str, Any]:
    return {
        "story_id": strategy.strategy_id,
        "role": role,
        "budget_share": budget_share,
        "angle": strategy.sub_angle or strategy.type,
        "purchase_reason": strategy.core_commercial_idea or strategy.thesis,
        "natural_duration_seconds": round(float(strategy.recommended_duration_seconds or 0.0), 3),
    }


def _proposal(
    mode: str, primary: Strategy, supporting: Strategy | None = None,
    narrative_archetype: str = "pain_point",
) -> dict[str, Any]:
    copy = dict(_MODE_COPY[mode])
    mix = [_mix_item(primary, "primary", 0.7 if supporting else 1.0)]
    if supporting and supporting.strategy_id != primary.strategy_id:
        mix.append(_mix_item(supporting, "purchase_support", 0.3))
    duration = sum(float(item["natural_duration_seconds"] or 0.0) * float(item["budget_share"]) for item in mix)
    angle = primary.sub_angle or primary.type
    blueprint_contract = build_narrative_blueprint_contract(primary, narrative_archetype)
    return {
        "director_strategy_id": f"D_{mode.upper()}_{primary.strategy_id}",
        "director_mode": mode,
        "available": True,
        "primary_story_id": primary.strategy_id,
        "supporting_story_ids": [item["story_id"] for item in mix[1:]],
        "opening_promise": primary.core_commercial_idea or primary.thesis,
        "commercial_goal": copy["goal"],
        "why_this_plan": f"以 {primary.strategy_id} 的“{angle}”作为独立购买承诺。",
        "estimated_natural_duration": round(duration, 3),
        "story_mix": mix,
        "name": f"{copy['name']} · {angle}",
        **blueprint_contract,
        **copy,
    }


def _single_pass_proposal(strategy: Strategy, *, plan_role: str = "") -> dict[str, Any]:
    """Expose an AI-authored director packet without re-templating it.

    The response already contains the story, desired purchase journey and the
    exact source utterance order.  This function deliberately does *not* rank
    clips, regenerate an archetype, or substitute a canned commercial mode.
    It only turns that one response into an operator-visible proposal.
    """
    title = strategy.director_title or strategy.core_commercial_idea or strategy.thesis or strategy.strategy_id
    core_desire = strategy.core_desire or strategy.core_commercial_idea or strategy.thesis
    is_primary = str(plan_role or strategy.director_plan_role or "primary").lower() != "alternative"
    has_packet = len(strategy.director_sequence) >= 2
    video_structure = {
        "id": strategy.video_structure_id or strategy.narrative_archetype or "director_defined",
        "name": strategy.video_structure_name or strategy.narrative_archetype or "导演自定义结构",
        "selection_reason": strategy.video_structure_reason,
    }
    return {
        "director_strategy_id": f"D_DIRECTOR_{strategy.strategy_id}",
        "director_mode": "single_pass_director",
        "available": has_packet,
        "unavailable_reason": (
            "主方案缺少至少两段真实口播，未进入物化。"
            if not has_packet else ""
        ),
        "primary_story_id": strategy.strategy_id,
        "supporting_story_ids": [],
        "opening_promise": strategy.opening_promise or core_desire,
        "commercial_goal": core_desire,
        "core_desire": core_desire,
        "narrative_archetype": strategy.narrative_archetype or "director_defined",
        "why_this_plan": strategy.thesis or core_desire,
        "estimated_natural_duration": round(float(strategy.recommended_duration_seconds or 0.0), 3),
        "story_mix": [_mix_item(strategy, "primary", 1.0)],
        "name": title,
        "icon": "🎬",
        "headline": strategy.opening_promise or core_desire,
        "quality_tier": strategy.director_quality_tier or "standard",
        "video_structure": video_structure,
        "director_plan_role": "primary" if is_primary else "alternative",
        "requires_additional_ai_call": not has_packet,
        "materialization_status": "ready" if has_packet else "direction_only",
        "director_sequence": [item.to_dict() for item in strategy.director_sequence] if has_packet else [],
        "chapter_packets": [item.to_dict() for item in strategy.director_chapter_packets] if has_packet else [],
        "final_readthrough": strategy.director_readthrough if has_packet else "",
        "opening_alternative_packages": [
            item.to_dict() for item in strategy.director_opening_alternatives
        ] if has_packet else [],
        "whole_video_audit": dict(strategy.whole_video_audit or {}) if has_packet else {},
        "opening_scope": {
            "source": "single_ai_director_packet",
            **dict(strategy.opening_scope or {}),
        },
        "blueprint": {
            "version": "single-ai-director-packet-v2-full-safe-pool-journey",
            "duration_policy": "natural_30_to_120_when_source_supports_it",
            "stop_rule": "AI_stops_when_no_new_purchase_value_remains",
            "journey_contract": "one_ai_director_declares_purchase_questions_and_exact_source_ids",
            "video_structure": video_structure,
            "chapter_packet_contract": "one_ai_director_declares_complete_micro_narrative_chapters",
        },
    }


def build_director_strategy_library(strategies: Sequence[Strategy]) -> dict[str, Any]:
    """Select one to three materially different sell paths from M1 only."""
    # New preview requests are resolved in one semantic call.  Any packet in
    # that response is authoritative: do not turn it back into the historical
    # traffic/conversion/premium cards and then call M2 repeatedly.
    single_pass = [
        story for story in strategies
        if story.director_sequence or str(story.director_plan_role or "").lower() == "alternative"
    ]
    if single_pass:
        quality_rank = {"strong": 0, "standard": 1, "basic": 2}
        primary = [story for story in single_pass if str(story.director_plan_role or "").lower() == "primary"]
        if not primary:
            primary = [story for story in single_pass if story.director_sequence][:1]
        alternatives = [story for story in single_pass if story not in primary]
        proposals = [
            *[_single_pass_proposal(story, plan_role="primary") for story in primary[:1]],
            *[_single_pass_proposal(story, plan_role="alternative") for story in alternatives[:2]],
        ]
        return {
            "version": "director-strategy-single-pass-v1",
            "proposal_count": len(proposals),
            "proposals": proposals,
            "boundary": {
                "single_ai_director_packet": True,
                "m1_discovery_reused": True,
                "no_new_claims": True,
                "no_program_candidate_selection": True,
                "no_program_candidate_ordering": True,
                "fixed_mode_templates_used": False,
                "operator_selection_required": True,
            },
        }
    approved = sorted((story for story in strategies if _automatic_eligible(story)), key=_priority_key)
    if not approved:
        approved = sorted(strategies, key=_priority_key)
    primary = next((story for story in approved if not _has_marker(story, _QUALITY_MARKERS)), approved[0] if approved else None)
    quality = next((story for story in approved if _has_marker(story, _PREMIUM_MARKERS)), None)
    utility = _distinct(primary, approved, _UTILITY_MARKERS) if primary else None
    conversion_primary = utility or (_distinct(primary, approved) if primary else None)
    proposals: list[dict[str, Any]] = []
    if primary:
        proposals.append(_proposal("traffic", primary, narrative_archetype="pain_point"))
    if conversion_primary and conversion_primary.strategy_id != (primary.strategy_id if primary else ""):
        proposals.append(_proposal("conversion", conversion_primary, primary, narrative_archetype="pain_point"))
    if quality and quality.strategy_id not in {item["primary_story_id"] for item in proposals}:
        proposals.append(_proposal("premium", quality, primary, narrative_archetype="scene_immersion"))
    proposals = proposals[:3]
    return {
        "version": "director-strategy-layer-v2-narrative-blueprint-p0",
        "proposal_count": len(proposals),
        "proposals": proposals,
        "boundary": {
            "m1_discovery_reused": True,
            "no_new_claims": True,
            "no_candidate_selection": True,
            "no_candidate_ordering": True,
            "operator_selection_required": True,
            "automatic_story_composition": False,
            "distinct_primary_promise_required": True,
        },
    }


def _unique_evidence(items: Sequence[EvidenceItem]) -> tuple[EvidenceItem, ...]:
    result: list[EvidenceItem] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for item in items:
        key = (item.claim, tuple(item.subtitle_ids))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def compile_director_strategy(
    proposal: Mapping[str, Any],
    strategies: Sequence[Strategy],
) -> tuple[Strategy, dict[str, Any]]:
    """Compile one operator-selected proposal into M2's existing story shape.

    The returned Strategy is a transparent union of specified M1 stories.  It
    carries a director-owned ID, while ``source_story_ids`` in the companion
    contract keeps the original discovery lineage visible for audits.
    """
    if not bool(proposal.get("available")):
        raise ValueError(str(proposal.get("unavailable_reason") or "该导演方案当前没有可验证故事。"))
    by_id = {story.strategy_id: story for story in strategies}
    mix = list(proposal.get("story_mix") or [])
    source_ids = [str(item.get("story_id") or "") for item in mix if isinstance(item, Mapping)]
    source = [by_id[story_id] for story_id in source_ids if story_id in by_id]
    if not source:
        raise ValueError("导演方案没有可复用的 M1 故事。")
    primary = source[0]
    primary_core = tuple(primary.core_evidence_pool or primary.evidence)
    extra_support = tuple(item for story in source[1:] for item in (story.core_evidence_pool or story.evidence))
    supporting = _unique_evidence((*primary.supporting_evidence_pool, *extra_support, *(item for story in source[1:] for item in story.supporting_evidence_pool)))
    bridge = _unique_evidence(tuple(item for story in source for item in story.bridge_candidates))
    composite = replace(
        primary,
        strategy_id=str(proposal.get("director_strategy_id") or primary.strategy_id),
        # ``director_mode`` is presentation and task context, not an M1 fact.
        # Replacing the primary story's thesis/promise with a generic card copy
        # (for example "成交转化版") made M2 lose the actual purchase claim it
        # must compose.  Keep the selected M1 story intact; the companion
        # contract below carries the director-mode metadata separately.
        core_evidence_pool=primary_core,
        supporting_evidence_pool=supporting,
        bridge_candidates=bridge,
        evidence=_unique_evidence((*primary_core, *supporting, *bridge)),
        content_dependencies=tuple(sorted({item for story in source for item in story.content_dependencies})),
    )
    contract = {
        "version": (
            "director-strategy-single-pass-contract-v1"
            if str(proposal.get("director_mode") or "") == "single_pass_director"
            else "director-strategy-contract-v2-narrative-blueprint-p0"
        ),
        "director_strategy_id": composite.strategy_id,
        "director_mode": str(proposal.get("director_mode") or ""),
        "source_story_ids": source_ids,
        "story_mix": mix,
        "m2_role": "compose_only",
        "m3_role": "unchanged_word_level_materialization_only",
        "no_new_claims": True,
        "narrative_archetype": str(proposal.get("narrative_archetype") or ""),
        "core_desire": str(proposal.get("core_desire") or ""),
        "blueprint": dict(proposal.get("blueprint") or {}),
        "single_ai_director_packet": str(proposal.get("director_mode") or "") == "single_pass_director",
        "director_title": str(proposal.get("name") or ""),
        "opening_promise": str(proposal.get("opening_promise") or ""),
        "opening_scope": dict(proposal.get("opening_scope") or {}),
        "director_quality_tier": str(proposal.get("quality_tier") or ""),
        "video_structure": dict(proposal.get("video_structure") or {}),
        "chapter_packets": [
            dict(item) for item in proposal.get("chapter_packets") or () if isinstance(item, Mapping)
        ],
        "opening_alternative_packages": [
            dict(item) for item in proposal.get("opening_alternative_packages") or ()
            if isinstance(item, Mapping)
        ],
        "whole_video_audit": dict(proposal.get("whole_video_audit") or {}),
        "director_sequence": [
            dict(item) for item in proposal.get("director_sequence") or () if isinstance(item, Mapping)
        ],
    }
    return composite, contract
