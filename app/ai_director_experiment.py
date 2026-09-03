"""Explicit, non-user-facing switches for controlled AI-director experiments.

This module deliberately has no preview, export, or server-route integration.
It only tells a source runner whether the operator explicitly enabled the
experimental M1 -> M2 -> M3 branch in local settings, and which M2 planner
variant that runner may use.  These values never select a user-preview,
formal-export, or publication route.
"""

from __future__ import annotations

from typing import Any, Mapping


AI_DIRECTOR_MODE_LEGACY = "legacy"
AI_DIRECTOR_MODE_EXPERIMENTAL = "experimental"
AI_DIRECTOR_MODE_PRODUCTION = "production"
AI_DIRECTOR_MODES = frozenset((
    AI_DIRECTOR_MODE_LEGACY,
    AI_DIRECTOR_MODE_EXPERIMENTAL,
    AI_DIRECTOR_MODE_PRODUCTION,
))


# The planner decision is intentionally narrower than ``ai_director_mode``:
# it changes only the M2 composition step inside a controlled source runner.
# Missing or malformed settings must preserve the existing planner.
M2_PLANNER_MODE_LEGACY = "legacy"
M2_PLANNER_MODE_HEAVY_DIRECTOR = "heavy_director"
M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT = "lite_director_experiment"
M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT = "lite_chapter_compression_experiment"
M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT = "lite_final_editor_experiment"
M2_PLANNER_MODES = frozenset((
    M2_PLANNER_MODE_LEGACY,
    M2_PLANNER_MODE_HEAVY_DIRECTOR,
    M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT,
    M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT,
    M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT,
))

M2_PLANNER_VERSIONS = {
    M2_PLANNER_MODE_LEGACY: "legacy_m2",
    M2_PLANNER_MODE_HEAVY_DIRECTOR: "commerce_director_v1",
    # v2.1 denotes the Lite Plan + execution-only metadata-adapter contract.
    M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT: "commerce_lite_v2.1",
    # A separate, operator-only branch: initial Lite plan followed by one
    # chapter-compression pass. It never changes the normal preview route.
    M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT: "commerce_lite_m2.4_chapter_compression",
    # The existing operator-only route hosts Narrative Mode: P0.5A.2 source
    # inventory -> P0.5A.3 calibrated Actor Pool -> Director Journey ->
    # P0.5A.4 AI-approved Hook/Payoff Package -> AI Beat Casting -> Whole
    # Video Audit. The setting name stays stable so no UI redesign is needed,
    # and M3 remains the unchanged word-level materializer.
    M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT: "commerce_m2_narrative_beat_casting_p0_5a3_a4_v3",
}


def resolve_ai_director_mode(settings: Mapping[str, Any] | None) -> str:
    """Return a fail-closed local mode; missing or malformed means legacy."""
    raw = str((settings or {}).get("ai_director_mode") or "").strip().lower()
    return raw if raw in AI_DIRECTOR_MODES else AI_DIRECTOR_MODE_LEGACY


def controlled_experiment_enabled(settings: Mapping[str, Any] | None) -> bool:
    """Experiments need an explicit persisted opt-in, never a CLI default."""
    return resolve_ai_director_mode(settings) == AI_DIRECTOR_MODE_EXPERIMENTAL


def resolve_m2_planner_mode(settings: Mapping[str, Any] | None) -> str:
    """Return a fail-closed planner mode; malformed values retain legacy M2."""
    raw = str((settings or {}).get("m2_planner_mode") or "").strip().lower()
    return raw if raw in M2_PLANNER_MODES else M2_PLANNER_MODE_LEGACY


def controlled_planner_mode(settings: Mapping[str, Any] | None) -> str:
    """Expose an experimental planner only under the existing explicit opt-in."""
    if not controlled_experiment_enabled(settings):
        return M2_PLANNER_MODE_LEGACY
    return resolve_m2_planner_mode(settings)


def planner_mode_version(mode: str) -> str:
    """Stable manifest label; unknown modes are represented as legacy."""
    return M2_PLANNER_VERSIONS.get(str(mode or "").strip(), M2_PLANNER_VERSIONS[M2_PLANNER_MODE_LEGACY])


def experimental_output_policy(*, technical_ok: bool) -> dict[str, Any]:
    """Separate diagnostic generation from human release approval."""
    return {
        "mode": AI_DIRECTOR_MODE_EXPERIMENTAL,
        "output_allowed": bool(technical_ok),
        "user_preview_allowed": False,
        "formal_export_allowed": False,
        "publication_allowed": False,
        "opening_review_is_non_blocking": True,
        "human_commercial_review_required": True,
    }
