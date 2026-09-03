"""Read-only M1 Story Library projection for commercial-director review.

M1 may discover several valid purchase stories from the same frozen source.
This adapter makes that fact visible without selecting one story, filtering the
candidate pool, or authorizing M2 to combine stories.  It is deliberately a
review artifact, not a planner input.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _candidate_index(candidates: Sequence[Any]) -> dict[int, list[dict[str, Any]]]:
    """Index immutable candidates by their recorded semantic subtitle lineage."""
    by_subtitle: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidates:
        candidate_id = int(getattr(candidate, "candidate_id", 0) or 0)
        for subtitle_id in tuple(getattr(candidate, "origin_subtitle_ids", ()) or ()):
            try:
                key = int(subtitle_id)
            except (TypeError, ValueError):
                continue
            by_subtitle.setdefault(key, []).append({
                "candidate_id": candidate_id,
                "start": round(float(getattr(candidate, "start", 0.0) or 0.0), 3),
                "end": round(float(getattr(candidate, "end", 0.0) or 0.0), 3),
                "duration_seconds": round(float(getattr(candidate, "duration", 0.0) or 0.0), 3),
                "text": str(getattr(candidate, "text", "") or ""),
            })
    return by_subtitle


def _evidence_payload(evidence: Any, tier: str, candidate_by_subtitle: Mapping[int, list[dict[str, Any]]]) -> dict[str, Any]:
    subtitle_ids = [int(item) for item in tuple(getattr(evidence, "subtitle_ids", ()) or ())]
    linked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for subtitle_id in subtitle_ids:
        for candidate in candidate_by_subtitle.get(subtitle_id, ()):
            candidate_id = int(candidate["candidate_id"])
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            linked.append(dict(candidate))
    return {
        "asset_tier": tier,
        "role": str(getattr(evidence, "role", "") or ""),
        "claim": str(getattr(evidence, "claim", "") or ""),
        "evidence_basis": str(getattr(evidence, "evidence_basis", "explicit") or "explicit"),
        "subtitle_ids": subtitle_ids,
        "candidate_lineage": linked,
        "candidate_lineage_status": "resolved" if linked else "unresolved",
    }


def _story_payload(strategy: Any, candidate_by_subtitle: Mapping[int, list[dict[str, Any]]]) -> dict[str, Any]:
    core = tuple(getattr(strategy, "core_evidence_pool", ()) or getattr(strategy, "evidence", ()) or ())
    supporting = tuple(getattr(strategy, "supporting_evidence_pool", ()) or ())
    bridge = tuple(getattr(strategy, "bridge_candidates", ()) or ())
    assets = [
        *(_evidence_payload(item, "core", candidate_by_subtitle) for item in core),
        *(_evidence_payload(item, "supporting", candidate_by_subtitle) for item in supporting),
        *(_evidence_payload(item, "bridge", candidate_by_subtitle) for item in bridge),
    ]
    hook = next((item for item in assets if item["role"] == "hook"), assets[0] if assets else {})
    return {
        "story_id": str(getattr(strategy, "strategy_id", "") or ""),
        "angle": str(getattr(strategy, "sub_angle", "") or getattr(strategy, "type", "") or ""),
        "strategy_family": str(getattr(strategy, "strategy_family", "") or ""),
        "story_priority": str(getattr(strategy, "story_priority", "") or ""),
        "story_validity": str(getattr(strategy, "story_validity", "") or ""),
        "target_audience": str(getattr(strategy, "target_user", "") or ""),
        "hook": dict(hook),
        "pain": str(getattr(strategy, "audience_tension", "") or getattr(strategy, "story_trigger", "") or ""),
        "purchase_reason": str(getattr(strategy, "core_commercial_idea", "") or getattr(strategy, "thesis", "") or ""),
        "payoff": str(getattr(strategy, "payoff", "") or ""),
        "natural_duration_seconds": round(float(getattr(strategy, "recommended_duration_seconds", 0.0) or 0.0), 3),
        "duration_feasibility": str(getattr(strategy, "duration_feasibility", "unknown") or "unknown"),
        "content_dependencies": list(tuple(getattr(strategy, "content_dependencies", ()) or ())),
        "excluded_assets_reason": list(tuple(getattr(strategy, "excluded_assets_reason", ()) or ())),
        "assets": assets,
    }


def build_story_library(strategies: Sequence[Any], safe_candidates: Sequence[Any]) -> dict[str, Any]:
    """Project all M1-approved stories with explicit candidate lineage.

    No story is ranked, removed, composed, or offered as an M2 allow-list.
    ``candidate_lineage_status=unresolved`` is intentionally visible rather
    than guessed from semantic similarity.
    """
    candidate_by_subtitle = _candidate_index(safe_candidates)
    stories = [_story_payload(strategy, candidate_by_subtitle) for strategy in strategies]
    return {
        "version": "m1-story-library-v1",
        "story_count": len(stories),
        "stories": stories,
        "boundary": {
            "discovery_only": True,
            "auto_story_selection": False,
            "auto_story_composition": False,
            "candidate_filtering": False,
            "lineage_policy": "recorded_subtitle_to_frozen_candidate_only",
        },
    }
