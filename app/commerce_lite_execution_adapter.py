"""Execution-only metadata adapter for Commerce Planner Lite.

The Lite ranker already chose chapter IDs and their order.  This adapter may
derive only the declarations that the common M2/M3 contract can verify from
those selections: opening IDs, supporting/bridge declaration IDs, and a
coverage map.  It cannot replace, append, remove, trim, or reorder a chapter
or candidate, and cannot invent any commercial explanation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from story_planner import (
    CommercialStoryBrief,
    NarrativePlan,
    OpeningPackage,
    PlanningCandidate,
    StoryConsumption,
    _parse_beats,
    _parse_depth_expansion,
    _parse_duration_assessment,
    _parse_duration_plan,
    _parse_opening_package,
    _parse_story_consumption,
    bind_story_assets,
    validate_narrative_plan,
)


COMMERCE_LITE_EXECUTION_ADAPTER_VERSION = "commerce-lite-execution-adapter-v1"


def _chapter_snapshot(plan: NarrativePlan) -> list[dict[str, Any]]:
    """Only directing fields: used to prove no semantic selection mutation."""
    return [
        {
            "chapter_id": beat.chapter_id,
            "narrative_role": beat.narrative_role,
            "candidate_ids": list(beat.candidate_ids),
            "goal": beat.goal,
            "story_support": beat.story_support,
            "selection_instruction": beat.selection_instruction,
            "commerce_beat_id": beat.commerce_beat_id,
            "value_dimension": beat.value_dimension,
            "purchase_value_dimension": beat.purchase_value_dimension,
            "purchase_value_domain": beat.purchase_value_domain,
            "purchase_value_outcomes": list(beat.purchase_value_outcomes),
            "purchase_value_reason": beat.purchase_value_reason,
        }
        for beat in plan.beats
    ]


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _opening_declaration(opening: OpeningPackage | None) -> dict[str, list[int]]:
    return {
        "hook_candidate_ids": list(opening.hook_candidate_ids) if opening else [],
        "payoff_candidate_ids": list(opening.payoff_candidate_ids) if opening else [],
    }


def _consumption_declaration(consumption: StoryConsumption | None) -> dict[str, list[Any]]:
    return {
        "supporting_chapter_ids": list(consumption.supporting_chapter_ids) if consumption else [],
        "bridge_chapter_ids": list(consumption.bridge_chapter_ids) if consumption else [],
        "supporting_candidate_ids": list(consumption.supporting_candidate_ids) if consumption else [],
        "bridge_candidate_ids": list(consumption.bridge_candidate_ids) if consumption else [],
    }


def narrative_plan_from_mapping(raw: Mapping[str, Any], *, strategy: Any) -> NarrativePlan:
    """Restore a durable Lite Plan for execution-only replay; no model call."""
    target_duration = float(raw.get("target_duration") or 45.0)
    return NarrativePlan(
        strategy_id=str(raw.get("strategy_id") or getattr(strategy, "strategy_id", "")),
        thesis=str(raw.get("thesis") or getattr(strategy, "thesis", "")),
        target_duration=target_duration,
        beats=_parse_beats(raw),
        status=str(raw.get("status") or "insufficient_material"),
        recommended_duration=float(raw.get("recommended_duration") or 0.0),
        issues=tuple(str(item) for item in raw.get("issues") or ()),
        removed_beats=(),
        plan_valid=bool(raw.get("plan_valid", True)),
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=_parse_opening_package(raw.get("opening_package")),
        selection_contract=dict(raw.get("selection_contract") or {}),
        selected_candidates=tuple(
            PlanningCandidate.from_mapping(item)
            for item in raw.get("selected_candidates") or ()
            if isinstance(item, Mapping)
        ),
        duration_assessment=_parse_duration_assessment(raw.get("duration_assessment")),
        duration_plan=_parse_duration_plan(raw.get("duration_plan"), target_duration=target_duration),
        depth_expansion=_parse_depth_expansion(raw.get("depth_expansion"), target_duration=target_duration),
        story_consumption=_parse_story_consumption(raw.get("story_consumption")),
    )


@dataclass(frozen=True)
class ExecutionMetadataAlignment:
    """Auditable before/after declaration record; never a selection repair."""

    status: str
    original: Mapping[str, Any]
    derived: Mapping[str, Any]
    differences: tuple[Mapping[str, Any], ...]
    coverage_chapter_ids: Mapping[str, tuple[str, ...]]
    directing_fingerprint_before: str
    directing_fingerprint_after: str
    selection_unchanged: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": COMMERCE_LITE_EXECUTION_ADAPTER_VERSION,
            "status": self.status,
            "original_plan": dict(self.original),
            "derived_from_actual_selection": dict(self.derived),
            "difference": [dict(item) for item in self.differences],
            "coverage_chapter_ids": {key: list(value) for key, value in self.coverage_chapter_ids.items()},
            "directing_fingerprint_before": self.directing_fingerprint_before,
            "directing_fingerprint_after": self.directing_fingerprint_after,
            "selection_unchanged": self.selection_unchanged,
            "allowed_changes": "declaration_metadata_only",
            "forbidden_changes": [
                "candidate_replace", "candidate_append", "candidate_remove", "candidate_reorder",
                "chapter_reorder", "opening_regeneration", "selling_point_addition",
            ],
            "notes": list(self.notes),
        }


def align_lite_execution_metadata(
    *,
    plan: NarrativePlan,
    strategy: Any,
    safe_candidates: Sequence[PlanningCandidate],
    executable_evidence: Mapping[int, Mapping[str, Any]],
) -> tuple[NarrativePlan, ExecutionMetadataAlignment]:
    """Derive declaration fields from existing Lite chapters, then validate.

    A missing Opening package or fewer than two chapters cannot be synthesized:
    that is an invalid planning result, not an adapter opportunity.
    """
    before_snapshot = _chapter_snapshot(plan)
    before_fingerprint = _fingerprint(before_snapshot)
    original = {
        "opening": _opening_declaration(plan.opening_package),
        "story_consumption": _consumption_declaration(plan.story_consumption),
    }
    if plan.opening_package is None or len(plan.beats) < 2:
        report = ExecutionMetadataAlignment(
            status="refused_missing_opening_or_two_chapters",
            original=original,
            derived={},
            differences=(),
            coverage_chapter_ids={},
            directing_fingerprint_before=before_fingerprint,
            directing_fingerprint_after=before_fingerprint,
            selection_unchanged=True,
            notes=("Adapter cannot invent a missing opening declaration or second payoff chapter.",),
        )
        return plan, report

    brief = CommercialStoryBrief.from_strategy(strategy)
    annotated_candidates = bind_story_assets(brief, tuple(safe_candidates))
    candidate_by_id = {candidate.candidate_id: candidate for candidate in annotated_candidates}
    supporting_chapters: list[str] = []
    bridge_chapters: list[str] = []
    supporting_ids: list[int] = []
    bridge_ids: list[int] = []
    coverage: dict[str, list[str]] = {}
    for beat in plan.beats:
        chapter_id = str(beat.chapter_id or "")
        tiers = {
            tier
            for candidate_id in beat.candidate_ids
            for tier in tuple(candidate_by_id.get(candidate_id, PlanningCandidate(0, "", 0, 0, "")).asset_tiers)
        }
        if "supporting" in tiers and chapter_id:
            supporting_chapters.append(chapter_id)
        if "bridge" in tiers and chapter_id:
            bridge_chapters.append(chapter_id)
        for candidate_id in beat.candidate_ids:
            candidate = candidate_by_id.get(candidate_id)
            if candidate is None:
                continue
            if "supporting" in candidate.asset_tiers and candidate_id not in supporting_ids:
                supporting_ids.append(candidate_id)
            if "bridge" in candidate.asset_tiers and candidate_id not in bridge_ids:
                bridge_ids.append(candidate_id)
        if beat.commerce_beat_id and chapter_id:
            coverage.setdefault(beat.commerce_beat_id, []).append(chapter_id)

    original_consumption = plan.story_consumption
    derived_consumption = StoryConsumption(
        hero_strategy_id=str(getattr(strategy, "strategy_id", "")),
        hero_priority=str(getattr(strategy, "story_priority", "")),
        hero_consistency_reason=(
            original_consumption.hero_consistency_reason if original_consumption else ""
        ),
        supporting_chapter_ids=tuple(supporting_chapters),
        bridge_chapter_ids=tuple(bridge_chapters),
        no_rediscovery=bool(original_consumption.no_rediscovery) if original_consumption else False,
        supporting_candidate_ids=tuple(supporting_ids),
        bridge_candidate_ids=tuple(bridge_ids),
    )
    opening = replace(
        plan.opening_package,
        hook_candidate_ids=tuple(plan.beats[0].candidate_ids),
        payoff_candidate_ids=tuple(plan.beats[1].candidate_ids),
    )
    derived = {
        "opening": _opening_declaration(opening),
        "story_consumption": _consumption_declaration(derived_consumption),
    }
    differences: list[dict[str, Any]] = []
    for scope in ("opening", "story_consumption"):
        for field, value in dict(derived[scope]).items():
            prior = dict(original[scope]).get(field)
            if prior != value:
                differences.append({
                    "field": f"{scope}.{field}",
                    "original": prior,
                    "derived": value,
                    "reason": "metadata_alignment_only_from_actual_selected_chapters",
                })
    aligned = replace(plan, opening_package=opening, story_consumption=derived_consumption)
    after_snapshot = _chapter_snapshot(aligned)
    after_fingerprint = _fingerprint(after_snapshot)
    report = ExecutionMetadataAlignment(
        status="aligned",
        original=original,
        derived=derived,
        differences=tuple(differences),
        coverage_chapter_ids={key: tuple(value) for key, value in coverage.items()},
        directing_fingerprint_before=before_fingerprint,
        directing_fingerprint_after=after_fingerprint,
        selection_unchanged=before_snapshot == after_snapshot,
        notes=(
            "Only contract declarations were derived from actual chapter candidate IDs.",
            "The common Validator remains authoritative for every non-metadata defect.",
        ),
    )
    # The common validator owns timestamp-derived duration facts, but it knows
    # nothing about the M2 Purchase Journey audit. Preserve that M2 contract
    # verbatim through this execution-only adapter; it neither changes a
    # candidate nor gives M3 a new editing instruction.
    validated = validate_narrative_plan(
        aligned, annotated_candidates, executable_evidence=executable_evidence,
    )
    assessment = dict(validated.duration_assessment or {})
    for key in (
        "commerce_strong_clip_ranking",
        "commerce_purchase_cognition_path",
        "commerce_purchase_journey",
        "commerce_purchase_journey_quality",
        "commerce_chapter_packet_builder",
        "commerce_narrative_enrichment",
    ):
        if key in dict(plan.duration_assessment or {}):
            assessment[key] = dict(plan.duration_assessment or {})[key]
    # ``validate_narrative_plan`` measures duration correctly but has no
    # Purchase Journey Completion context.  Preserve the already-passed M2
    # natural-ending status through this metadata-only adapter; candidates and
    # chapters are unchanged, and a journey-incomplete plan can never reach
    # this branch as a valid render handoff.
    source_assessment = dict(plan.duration_assessment or {})
    if (
        plan.plan_valid
        and plan.status == "journey_complete"
        and str(source_assessment.get("status") or "").strip() in {
            "natural_complete_below_target", "enriched_natural_complete", "packet_enriched_natural_complete",
        }
    ):
        for key in ("status", "reason", "duration_note", "actual_seconds", "preferred_low", "preferred_high"):
            if key in source_assessment:
                assessment[key] = source_assessment[key]
    journey_statuses = {
        "journey_complete",
        "journey_incomplete",
        "source_material_insufficient",
    }
    journey_audit = dict(plan.duration_assessment or {}).get("commerce_purchase_journey") or {}
    if not plan.plan_valid or plan.status == "journey_incomplete" or str(journey_audit.get("journey_status") or "").strip() == "journey_incomplete":
        # A metadata adapter must never turn an invalid M2 journey (including
        # a malformed question dependency) into an M3-executable plan merely
        # because its selected IDs happen to be materializable.
        return replace(
            validated,
            status="journey_incomplete" if plan.status in journey_statuses else plan.status,
            plan_valid=False,
            issues=tuple(dict.fromkeys(tuple(validated.issues) + tuple(plan.issues))),
            replan_request=plan.replan_request or validated.replan_request,
            duration_assessment=assessment,
        ), report
    return replace(
        validated,
        status=plan.status if plan.status in journey_statuses else validated.status,
        duration_assessment=assessment,
    ), report
