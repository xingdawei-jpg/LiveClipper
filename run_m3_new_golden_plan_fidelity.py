# -*- coding: utf-8 -*-
"""Source-only M1 -> M2 -> Binder -> M3 Plan Fidelity validation.

The runner exists only for the newly verified M3 golden sources.  It never
calls preview, rendering, or the production clip route.  M3 receives exactly
the candidates selected by M2 and may only materialize their verified word
spans; a blocked residual is reported back as an M2 replan requirement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from candidate_ledger import CandidateLedger  # noqa: E402
from clip_selector import (  # noqa: E402
    SelectorBlocked,
    assess_candidate_materializability,
    audit_materialization_fidelity,
    materialize_narrative_plan,
)
from commercial_analyzer import (  # noqa: E402
    Strategy,
    analyze_commercial_story,
    director_target_duration_range,
    director_delivery_duration_range,
    normalize_director_controls,
)
from commercial_asset_audit import classify_commercial_assets  # noqa: E402
from director_strategy import (  # noqa: E402
    build_director_strategy_library,
    build_narrative_blueprint_contract,
)
from story_library import build_story_library  # noqa: E402
from commerce_director import (  # noqa: E402
    audit_commerce_story_coverage,
    build_commerce_evidence_cards,
    plan_commerce_story_llm,
)
from commerce_planner_lite import (  # noqa: E402
    build_commerce_lite_tags,
    plan_commerce_lite_draft_final_llm,
    plan_commerce_lite_draft_rank_final_llm,
    plan_commerce_lite_chapter_compression_llm,
    plan_commerce_lite_final_editor_llm,
    plan_commerce_lite_narrative_mode_llm,
    plan_commerce_lite_strong_clip_llm,
    plan_commerce_lite_llm,
)
from commerce_lite_execution_adapter import align_lite_execution_metadata  # noqa: E402
from single_pass_director import build_single_pass_director_plan  # noqa: E402
from content_policy import default_content_policy, normalize_content_policy  # noqa: E402
from run_m1_asset_aware_goldens import _hard_safe_subtitles  # noqa: E402
from run_m2_story_consumption_validation import _planning_candidates  # noqa: E402
from run_m2_story_goldens import _load_srt  # noqa: E402
from run_m3_golden_source_identity import M3_GOLDEN_SOURCES  # noqa: E402
from semantic_word_binder import bind_candidates_by_semantic_srt, build_semantic_srt_word_timeline  # noqa: E402
from micro_beat_inventory import (  # noqa: E402
    adjudicate_micro_beat_publishability,
    build_micro_beat_source_rows,
    build_narrative_mode_beat_inventory,
    prepare_narrative_mode_beat_execution,
    reconstruct_frozen_micro_beat_candidates,
    replay_short_beat_contract_rejects,
)
from hook_opening_recall import (  # noqa: E402
    assemble_opening_packages,
    hook_candidate_as_opening_overlay,
    recall_hooks_from_complete_source,
)
from story_planner import (  # noqa: E402
    audit_story_consumption,
    finalize_duration_budget_after_retry,
    plan_narrative_llm,
    replan_narrative_llm,
    review_opening_quality_llm,
)
from story_planner import CommercialStoryBrief, build_executable_evidence_view  # noqa: E402


M3_GOLDEN_PRODUCTS = {
    "jccc_deep_roast_hoodie": "300G针织面料中长款抽绳廓形连帽卫衣",
    "shanjie_plaid_splice": "格纹拼接轻姿",
    "xiaoxian_relaxed_autumn_set": "松弛早秋连帽温柔奶茶色休闲套装",
}


def _load_frozen_m1_strategy(report_path: str) -> Strategy:
    """Reuse one previously approved same-source M1 brief for an M2 rerun."""
    path = Path(report_path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("selected_m1_hero") if isinstance(payload, Mapping) else None
    # Historical focused regressions store their one selected M1 hero beneath
    # ``cases``.  Reading that envelope is still a pure frozen-M1 reuse; it
    # must never fall back to a fresh M1 call simply because report shape
    # changed between P0.2 and P0.3.
    if not isinstance(raw, Mapping) and isinstance(payload, Mapping):
        cases = payload.get("cases")
        if isinstance(cases, Mapping) and len(cases) == 1:
            only_case = next(iter(cases.values()), None)
            raw = only_case.get("selected_m1_hero") if isinstance(only_case, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ValueError("冻结 M1 报告缺少 selected_m1_hero，不能重做或猜测 M1。")
    return Strategy.from_dict(raw, index=1)


def _asset_ledger_for_source(
    case_id: str,
    *,
    source_srt: str = "",
    content_policy: Mapping[str, Any] | None = None,
    allow_context_dependent_beats: bool = False,
) -> dict[str, Any]:
    """Build the same read-only hard-safe -> Commercial Asset Ledger boundary."""
    source_srt = str(source_srt or M3_GOLDEN_SOURCES[case_id]["srt"])
    srt_text, subtitles = _load_srt(source_srt)
    ledger = CandidateLedger()
    ledger.seed("semantic_input", subtitles)
    from ai_clipper import _build_ai_srt_entry_index, _director_safe_candidate_inventory  # noqa: E402

    inventory = _director_safe_candidate_inventory(
        _build_ai_srt_entry_index(srt_text),
        content_policy=normalize_content_policy(content_policy or default_content_policy()),
        allow_context_dependent_beats=allow_context_dependent_beats,
    )
    ledger.transition(
        "hard_safe_candidate_inventory", inventory,
        reason_code="product_agnostic_hard_safety",
    )
    assets = classify_commercial_assets(inventory)
    ledger.annotate_commercial_assets("commercial_asset_annotation", assets)
    inventory_by_id = {int(item.get("srt_index") or 0): dict(item) for item in inventory}
    rows: list[dict[str, Any]] = []
    for asset in assets:
        source = inventory_by_id.get(asset.candidate_id, {})
        rows.append({
            **asset.to_dict(),
            "source": str(source.get("source") or ""),
            "start": source.get("start"),
            "end": source.get("end"),
            "text": str(source.get("text") or ""),
        })
    return {
        "source_srt": source_srt,
        # P0.4 reads these immutable source units only as local context around
        # a selected anchor.  They are not a candidate pool and never give M3
        # permission to select or rewrite a subtitle.
        "source_context_units": [dict(item) for item in subtitles],
        "assets": rows,
        "hard_safe_candidate_count": len(rows),
        "candidate_ledger": ledger.to_dict(),
    }


def _select_m1_hero(strategies: Sequence[Any], requested_id: str = "") -> tuple[Any, str]:
    """Make the test's fixed M1 input explicit; this is not a product policy."""
    requested_id = str(requested_id or "").strip()
    if requested_id:
        selected = next((item for item in strategies if item.strategy_id == requested_id), None)
        if selected is None:
            raise ValueError(f"requested M1 strategy not found: {requested_id}")
        return selected, "explicit_strategy_id"
    selected = next(
        (item for item in strategies if str(getattr(item, "director_plan_role", "")).lower() == "primary"),
        None,
    )
    if selected is not None:
        return selected, "single_ai_primary_director_plan"
    selected = next((item for item in strategies if item.story_priority == "high"), None)
    if selected is not None:
        return selected, "first_m1_high_strategy_in_model_order"
    if strategies:
        return strategies[0], "first_m1_strategy_no_high_available"
    raise ValueError("M1 returned no strategy")


def _chapter_trace(plan, result, timeline) -> list[dict[str, Any]]:
    ranges = {(item.chapter_id, item.parent_candidate_id): item for item in result.ranges}
    blocked = {(item.chapter_id, item.parent_candidate_id): item for item in result.blocked}
    candidates = {item.candidate_id: item for item in plan.selected_candidates}
    spans = timeline.by_subtitle_id
    trace: list[dict[str, Any]] = []
    for beat in plan.beats:
        for candidate_id in beat.candidate_ids:
            candidate = candidates.get(candidate_id)
            origin_ids = tuple(candidate.origin_subtitle_ids) if candidate else ()
            trace.append({
                "chapter_id": beat.chapter_id,
                "story_support": beat.story_support,
                "asset_tier": beat.asset_tier,
                "candidate_id": candidate_id,
                "origin_subtitle_ids": list(origin_ids),
                "semantic_srt_word_spans": [
                    spans[item].to_dict() for item in origin_ids if item in spans
                ],
                "materialized_range": (
                    ranges[(beat.chapter_id, candidate_id)].to_dict()
                    if (beat.chapter_id, candidate_id) in ranges else None
                ),
                "selector_blocked": (
                    blocked[(beat.chapter_id, candidate_id)].to_dict()
                    if (beat.chapter_id, candidate_id) in blocked else None
                ),
            })
    return trace


def _mapping_rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _build_p05a3_runtime_actor_pool(
    *,
    p05a2_inventory: Mapping[str, Any],
    discovery_responses: Sequence[str],
    source_rows: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    boundary_response_hook: Any = None,
    adjudication_response_hook: Any = None,
) -> dict[str, Any]:
    """Apply the frozen P0.5A.3 short-Beat replay inside Narrative Mode.

    This reuses P0.5A's just-recorded discovery receipts.  It never conducts
    another discovery scan, scores a Beat, or chooses a final candidate.  If
    a prior run has no program-level Boundary reject, the replay is naturally
    empty and the one P0.5A.2 three-state adjudication still prepares the
    actor pool's current role/context permissions.
    """
    previous = dict(p05a2_inventory)
    old_contract_rejects = [
        item for item in _mapping_rows(previous.get("boundary_rejected_beats"))
        if str(item.get("decision") or "").strip().upper() != "REJECT"
    ]
    reconstructed: dict[str, Any] | None = None
    replay: dict[str, Any] = {
        "status": "not_needed_no_program_contract_rejects",
        "publishable_beat_inventory": [],
        "boundary_rejected_beats": [],
        "p0_5a3_short_recall_replay": {
            "frozen_contract_rejects_count": 0,
            "re_reviewed_count": 0,
        },
    }
    if old_contract_rejects:
        if not discovery_responses:
            return {
                "status": "p0_5a3_unavailable",
                "errors": ["p0_5a3_missing_frozen_p0_5a2_discovery_receipts"],
                "publishable_beat_inventory": [],
            }
        reconstructed = reconstruct_frozen_micro_beat_candidates(
            raw_inventory_responses=discovery_responses,
            source_rows=source_rows,
            allow_source_batch_layout_drift=True,
        )
        if reconstructed.get("status") != "frozen_inventory_reconstructed":
            return {
                "status": "p0_5a3_unavailable",
                "errors": list(reconstructed.get("errors") or ["p0_5a3_frozen_inventory_reconstruction_failed"]),
                "publishable_beat_inventory": [],
            }
        replay = replay_short_beat_contract_rejects(
            previous_inventory=previous,
            reconstructed_raw_inventory=reconstructed,
            source_rows=source_rows,
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            response_hook=boundary_response_hook,
        )
        if replay.get("status") != "publishable_inventory_completed":
            return {
                "status": "p0_5a3_unavailable",
                "errors": list(replay.get("errors") or ["p0_5a3_short_recall_failed"]),
                "publishable_beat_inventory": [],
            }
    combined_boundary = {
        "status": "publishable_inventory_completed",
        "publishable_beat_inventory": (
            _mapping_rows(previous.get("publishable_beat_inventory"))
            + _mapping_rows(replay.get("publishable_beat_inventory"))
        ),
        "boundary_rejected_beats": [
            item for item in _mapping_rows(previous.get("boundary_rejected_beats"))
            if str(item.get("decision") or "").strip().upper() == "REJECT"
        ] + _mapping_rows(replay.get("boundary_rejected_beats")),
        "boundary_statistics": dict(replay.get("boundary_statistics") or {}),
        "contract": dict(replay.get("contract") or {}),
    }
    calibrated = adjudicate_micro_beat_publishability(
        boundary_result=combined_boundary,
        api_key=str(settings["api_key"]),
        base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
        model=str(settings.get("model") or "deepseek-v4-flash"),
        response_hook=adjudication_response_hook,
    )
    if calibrated.get("status") != "publishable_inventory_calibrated":
        return {
            "status": "p0_5a3_unavailable",
            "errors": list(calibrated.get("errors") or ["p0_5a3_publishability_adjudication_failed"]),
            "publishable_beat_inventory": [],
        }
    calibrated["p0_5a3_short_recall_runtime"] = {
        "stage": "P0.5A.3 Short Beat Recall Calibration",
        "complete_srt_rescanned_for_discovery": False,
        "frozen_contract_rejects_count": len(old_contract_rejects),
        "re_reviewed_count": int((replay.get("p0_5a3_short_recall_replay") or {}).get("re_reviewed_count") or 0),
        "final_actor_pool_count": len(_mapping_rows(calibrated.get("publishable_beat_inventory"))),
        "final_usable_seconds": round(sum(
            float(item.get("duration_seconds") or 0.0)
            for item in _mapping_rows(calibrated.get("publishable_beat_inventory"))
        ), 3),
        "reconstruction_status": str((reconstructed or {}).get("status") or "not_needed"),
    }
    calibrated["contract"] = dict(calibrated.get("contract") or {}) | {
        "narrative_mode_actor_pool": "P0_5A3_calibrated_clean_visual",
        "strong_clip_ranking_used_before_journey": False,
        "semantic_selection_authority": "AI_director_then_AI_beat_casting",
        "program_authority": "P0_5A3_recall_status_role_permission_word_lineage_only",
    }
    return calibrated


def _narrative_candidates_from_execution(
    *, inventory: Mapping[str, Any], execution: Mapping[str, Any], disable_non_hook_openings: bool = False,
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """Attach source Beat audit facts to word-exact executable candidates only."""
    raw_by_beat_id = {
        str(item.get("beat_id") or ""): dict(item)
        for item in _mapping_rows(inventory.get("publishable_beat_inventory"))
    }
    mapped_by_candidate_id = {
        int(item.get("candidate_id") or 0): dict(item)
        for item in execution.get("beat_candidate_map") or () if isinstance(item, Mapping)
    }
    candidate_by_id = {
        int(candidate.candidate_id): candidate for candidate in execution.get("candidates") or ()
    }
    candidate_words = dict(execution.get("candidate_words") or {})
    revised_candidates: list[Any] = []
    rows: list[dict[str, Any]] = []
    for candidate_id, candidate in candidate_by_id.items():
        mapping = mapped_by_candidate_id.get(candidate_id, {})
        beat_id = str(mapping.get("beat_id") or "")
        is_special_hook = beat_id.startswith("H")
        selected_candidate = (
            replace(candidate, hook_eligible=bool(candidate.hook_eligible and is_special_hook))
            if disable_non_hook_openings else candidate
        )
        revised_candidates.append(selected_candidate)
        rows.append({
            **raw_by_beat_id.get(beat_id, {}),
            **mapping,
            "candidate_id": candidate_id,
            "duration_seconds": selected_candidate.duration,
            "hook_eligible": selected_candidate.hook_eligible,
            "planning_candidate": selected_candidate,
        })
    return {
        **dict(execution),
        "candidates": tuple(revised_candidates),
        "candidate_words": candidate_words,
        "beat_candidate_map": list(mapped_by_candidate_id.values()),
    }, tuple(rows)


def _write_approved_selector_render_manifest(
    *,
    output_dir: Path,
    case_id: str,
    source_srt: str,
    plan: Any,
    selector_result: Mapping[str, Any],
    quality_gate: Mapping[str, Any],
    approved: bool,
) -> Path | None:
    """Export a render handoff only after the approved M2/M3 receipts pass.

    This runner still does not encode video.  The existing offline selector
    renderer consumes this handoff later, so it cannot be pointed at a failed
    Purchase Journey Quality result by accident.
    """
    if not approved:
        return None
    selector = dict(selector_result or {})
    if str(selector.get("status") or "") != "ok" or not bool(quality_gate.get("passed")):
        return None
    source_path = Path(source_srt).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Approved render handoff missing source SRT: {source_path}")
    render_ready_dir = output_dir / f"{case_id}_m3_render_ready"
    render_ready_dir.mkdir(parents=True, exist_ok=True)
    # The renderer keeps this original source caption file only as lineage
    # evidence.  It writes a separate concatenated-output SRT during render.
    (render_ready_dir / "selector_subtitles.srt").write_text(
        source_path.read_text(encoding="utf-8"), encoding="utf-8",
    )
    ranges = [dict(item) for item in selector.get("ranges") or [] if isinstance(item, Mapping)]
    materialized = round(sum(float(item.get("duration") or 0.0) for item in ranges), 3)
    planned = round(float(getattr(plan, "total_seconds", materialized) or materialized), 3)
    duration_plan = getattr(plan, "duration_plan", None)
    manifest = {
        "case_id": case_id,
        "source_srt": str(source_path),
        "word_timeline_status": "semantic_srt_word_exact",
        "semantic_replan_required": False,
        "purchase_journey_quality_render_gate": dict(quality_gate),
        "duration_flow": {
            "target_duration": float(getattr(plan, "target_duration", 0.0) or 0.0),
            "planned_duration": planned,
            "materialized_duration": materialized,
            "materialization_delta": round(materialized - planned, 3),
            "duration_alignment_status": "aligned" if abs(materialized - planned) <= 0.03 else "mismatch",
            "duration_replan_required": False,
            "director_duration_plan": duration_plan.to_dict() if duration_plan else None,
        },
        "selector_result": selector,
        "render_authorization": "approved_m2_quality_gate_and_m3_fidelity",
    }
    manifest_path = render_ready_dir / "selector_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _run_case(
    case_id: str,
    *,
    settings: Mapping[str, Any],
    output_dir: Path,
    target_duration: float,
    duration_tolerance: float | None = None,
    skip_m3_materialization: bool = False,
    strategy_id: str,
    m2_replan_attempts: int,
    opening_quality_review: bool,
    commerce_director: bool = False,
    commerce_lite: bool = False,
    commerce_lite_draft_final: bool = False,
    commerce_lite_draft_rank_final: bool = False,
    commerce_lite_chapter_compression: bool = False,
    commerce_lite_final_editor: bool = False,
    director_strategy_discovery_only: bool = False,
    m1_strategy_override: Any | None = None,
    director_strategy_contract: Mapping[str, Any] | None = None,
    narrative_archetype: str = "",
    commerce_lite_replay_plan: Any | None = None,
    narrative_mode_inventory_override: Mapping[str, Any] | None = None,
    director_focus: Mapping[str, Any] | None = None,
    director_controls: Mapping[str, Any] | None = None,
    source_definition: Mapping[str, str] | None = None,
    director_progress_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if sum(bool(value) for value in (
        commerce_director, commerce_lite, commerce_lite_draft_final,
        commerce_lite_draft_rank_final, commerce_lite_chapter_compression, commerce_lite_final_editor,
    )) > 1:
        raise ValueError("heavy Commerce Director and Commerce Planner Lite are separate experimental variants")
    output_dir.mkdir(parents=True, exist_ok=True)
    source = dict(source_definition or M3_GOLDEN_SOURCES[case_id])
    label = str(source.get("label") or case_id)
    product = str(source.get("product") or M3_GOLDEN_PRODUCTS.get(case_id) or label)
    effective_director_controls = normalize_director_controls(director_controls)
    two_pass_director = bool(dict(director_strategy_contract or {}).get("two_pass_director_packet"))
    active_content_policy = normalize_content_policy(
        settings.get("content_policy") if isinstance(settings, Mapping) else default_content_policy()
    )
    ledger_context = _asset_ledger_for_source(
        case_id,
        source_srt=str(source["srt"]),
        content_policy=active_content_policy,
        allow_context_dependent_beats=two_pass_director,
    )
    assets = list(ledger_context["assets"])
    subtitles = _hard_safe_subtitles(assets)
    source_video = str(source.get("video") or "").strip()
    timeline = build_semantic_srt_word_timeline(
        ledger_context["source_srt"],
        source_video_path=source_video or None,
    )
    all_candidates = _planning_candidates(assets)
    all_candidate_words, all_candidate_word_rejections = (
        ({}, ())
        if skip_m3_materialization
        else bind_candidates_by_semantic_srt(all_candidates, timeline)
    )
    candidates = []
    materializability_blocked: list[dict[str, Any]] = []
    executable_evidence_facts: dict[int, dict[str, Any]] = {}
    for candidate in all_candidates:
        if skip_m3_materialization:
            candidates.append(candidate)
            executable_evidence_facts[candidate.candidate_id] = {
                "materializable": True,
                "materialization_issue": "",
                "execution_mode": "srt_sentence_preview_without_m3",
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            continue
        if candidate.candidate_id not in all_candidate_words:
            blocked = {
                "candidate_id": candidate.candidate_id,
                "code": "candidate_word_lineage_unbound",
                "detail": "candidate cannot be traced through verified semantic SRT word spans",
            }
            materializability_blocked.append(blocked)
            executable_evidence_facts[candidate.candidate_id] = {
                "materializable": False,
                "materialization_issue": f"{blocked['code']}:{blocked['detail']}",
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            continue
        checked = assess_candidate_materializability(candidate, all_candidate_words[candidate.candidate_id])
        if isinstance(checked, SelectorBlocked):
            blocked = checked.to_dict()
            materializability_blocked.append(blocked)
            executable_evidence_facts[candidate.candidate_id] = {
                "materializable": False,
                "materialization_issue": f"{blocked['code']}:{blocked['detail']}",
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            continue
        candidates.append(candidate)
        executable_evidence_facts[candidate.candidate_id] = {
            "materializable": True,
            "materialization_issue": "",
            "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
        }
    candidates = tuple(candidates)
    if not candidates:
        binder = timeline.report()
        binder_summary = {
            key: binder.get(key)
            for key in (
                "source_srt", "source_video", "word_sidecar", "source_identity",
                "total_srt_segments", "exact_aligned", "normalized_aligned", "ambiguous",
                "unmatched", "coverage", "word_count", "validation_issues",
            )
        }
        blocked_by_code: dict[str, int] = {}
        for item in materializability_blocked:
            code = str(item.get("code") or "unknown")
            blocked_by_code[code] = blocked_by_code.get(code, 0) + 1
        preflight = {
            "status": "blocked",
            "reason": (
                "no_hard_safe_sentence_candidates"
                if skip_m3_materialization else "no_materializable_hard_safe_candidates"
            ),
            "source_srt": ledger_context["source_srt"],
            "hard_safe_candidate_count": len(all_candidates),
            "word_lineage_bound_candidate_count": len(all_candidate_words),
            "materializable_candidate_count": 0,
            "binder": binder_summary,
            "candidate_word_lineage_rejections": list(all_candidate_word_rejections),
            "blocked_by_code": blocked_by_code,
            "blocked_candidate_sample": materializability_blocked[:20],
            "required_next_step": (
                "provide a same-video SRT with verified word-level .words.json and .asr-cache.json; "
                "do not reuse a sidecar from another video"
            ),
        }
        preflight_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.materialization_preflight.json"
        preflight_path.write_text(json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8")
        identity_reason = str((timeline.source_identity or {}).get("reason") or "unknown")
        if skip_m3_materialization:
            raise ValueError(f"{case_id}: no hard-safe SRT sentence candidates")
        raise ValueError(
            f"{case_id}: no materializable hard-safe candidates "
            f"(hard_safe={len(all_candidates)}, word_lineage={len(all_candidate_words)}, "
            f"binder_identity={identity_reason}; see {preflight_path.name}; "
            "the selected video needs its own verified word-level SRT sidecars)"
        )

    # The Director must choose from the same complete pool that M3 can bind.
    # This is the full materializable safe pool, never a Strong-Ranking slice
    # and never a program-selected story shortlist.
    executable_subtitle_ids = tuple(sorted({
        int(subtitle_id)
        for candidate in candidates
        for subtitle_id in (*candidate.origin_subtitle_ids, candidate.candidate_id)
        if int(subtitle_id) > 0
    }))

    # Materialization is an independent, deterministic prerequisite.  Do not
    # spend an M1 call when the selected source cannot ever reach M3.
    m1_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m1.response.txt"
    two_pass_response_paths: dict[str, str] = {}

    def capture_director_stage(stage: str, value: str) -> None:
        stage_name = {
            "initial_draft": "initial_draft",
            "final_revision": "final_revision",
            # Keep older artifacts readable when replaying an earlier caller.
            "story_contract": "story",
            "beat_casting": "casting",
        }.get(stage, str(stage or "unknown"))
        path = output_dir / m1_path.name.replace(
            ".m1.response.txt", f".director_{stage_name}.response.txt"
        )
        path.write_text(value, encoding="utf-8")
        two_pass_response_paths[stage] = path.name

    m1_result = None
    if m1_strategy_override is not None:
        strategy = m1_strategy_override
        hero_selection_reason = "frozen_same_source_m1_strategy_override"
        discovered_strategies = (strategy,)
    else:
        m1_result = analyze_commercial_story(
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            product=product,
            subtitles=subtitles,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            content_contract=active_content_policy,
            director_focus=director_focus,
            director_controls=effective_director_controls,
            commercial_assets=assets,
            executable_subtitle_ids=executable_subtitle_ids,
            raw_response_hook=lambda value: m1_path.write_text(value, encoding="utf-8"),
            stage_response_hook=capture_director_stage if two_pass_director else None,
            stage_progress_hook=director_progress_hook if two_pass_director else None,
            two_pass_director=two_pass_director,
            output_speed_factor=float(dict(director_strategy_contract or {}).get("output_speed_factor") or 1.0),
            source_context_subtitles=ledger_context.get("source_context_units") if two_pass_director else None,
            director_plan_count=max(
                1,
                min(3, int(dict(director_strategy_contract or {}).get("requested_director_plan_count") or 1)),
            ),
        )
        strategy, hero_selection_reason = _select_m1_hero(m1_result.strategies, strategy_id)
        discovered_strategies = tuple(m1_result.strategies)
    # M2 still receives exactly one explicitly selected M1 brief.  The library
    # is a side-output for human review: it must not alter that input or filter
    # the frozen hard-safe candidate pool.
    story_library = build_story_library(discovered_strategies, candidates)
    director_strategy_library = build_director_strategy_library(discovered_strategies)
    if narrative_archetype:
        # P0 Blueprint is authored by the existing Director Strategy layer;
        # this runner only makes that contract explicit for a frozen-M1
        # caramel regression.  It does not create a model call or alter M1.
        blueprint_contract = build_narrative_blueprint_contract(strategy, narrative_archetype)
        director_strategy_contract = {
            **dict(director_strategy_contract or {}),
            **blueprint_contract,
        }
    if director_strategy_discovery_only:
        return {
            "label": label,
            "product": product,
            "source_srt": ledger_context["source_srt"],
            "director_discovery_only": True,
            "passed": False,
            "m1_response": "reused_frozen_m1_strategy" if m1_result is None else m1_path.name,
            "m1_result": (
                {"reused_frozen_same_source_strategy": strategy.to_dict()}
                if m1_result is None else m1_result.to_dict()
            ),
            "two_pass_director_responses": dict(two_pass_response_paths),
            "director_controls": effective_director_controls,
            "selected_m1_hero": strategy.to_dict(),
            "m1_hero_selection_reason": hero_selection_reason,
            "m1_story_library": story_library,
            "director_strategy_library": director_strategy_library,
            "hard_safe_candidate_count": ledger_context["hard_safe_candidate_count"],
            "m2_materializable_hard_safe_candidate_count": len(candidates),
            "m2_materializability_preflight": {
                "blocked": materializability_blocked,
                "candidate_word_lineage_rejections": list(all_candidate_word_rejections),
            },
            "commercial_ready": False,
        }
    m2_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2.response.txt"
    duration_range = (
        director_delivery_duration_range(
            target_duration, duration_tolerance,
            float(dict(director_strategy_contract or {}).get("output_speed_factor") or 1.0),
        ) if two_pass_director else director_target_duration_range(target_duration, duration_tolerance)
    )
    contract = {
        "contract_version": "m3-new-golden-plan-fidelity-v1",
        "m1_hero_strategy_id": strategy.strategy_id,
        "m1_story_priority": strategy.story_priority,
        "m1_consumption_only": True,
        "m1_consumption_validation_require_supporting_bridge": True,
        "target_duration": float(target_duration),
        "duration_tolerance": duration_tolerance,
        "target_duration_range": dict(duration_range),
        "m3_word_lineage": "semantic_srt_word_binder_v1",
    }
    commerce_story_plan = None
    commerce_evidence_cards = ()
    commerce_director_response = ""
    commerce_lite_tags = ()
    commerce_lite_response = ""
    commerce_lite_draft_response = ""
    commerce_lite_draft = None
    commerce_lite_ranking_response = ""
    commerce_lite_ranking = None
    commerce_lite_compression_response = ""
    commerce_lite_final_editor_response = ""
    commerce_lite_final_editor_audit = None
    commerce_strong_clip_ranking_response = ""
    commerce_purchase_cognition_response = ""
    commerce_purchase_journey_recall_response = ""
    commerce_purchase_journey_quality_response = ""
    commerce_chapter_packet_response = ""
    commerce_narrative_enrichment_response = ""
    commerce_strong_clip_ranking = ()
    commerce_lite_source_plan = None
    commerce_lite_raw_plan = None
    commerce_lite_execution_adapter = None
    narrative_mode_beat_inventory = None
    narrative_mode_p05a3_actor_pool = None
    narrative_mode_beat_execution = None
    narrative_mode_beat_candidates: tuple[Any, ...] = ()
    narrative_mode_audit = None
    narrative_mode_response_paths: dict[str, Any] = {}
    narrative_mode_hook_recall = None
    narrative_mode_opening_packages = None
    director_variant_plans: list[dict[str, Any]] = []
    single_pass_director_execution = bool(
        commerce_lite_final_editor
        and bool(dict(director_strategy_contract or {}).get("single_ai_director_packet"))
    )
    # An empty/invalid modern packet must not fall through into historical
    # multi-prompt planners. The exact-source builder reports it as invalid.
    if commerce_director:
        # M2.5 reads existing commercial-asset facts and M1 evidence only.  It
        # outputs no IDs/times/order, and the returned plan is appended to M2's
        # contract as a coverage map rather than a candidate whitelist.
        commerce_evidence_cards = build_commerce_evidence_cards(
            strategy=strategy,
            ledger_assets=assets,
            executable_evidence=executable_evidence_facts,
        )
        commerce_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.commerce.response.txt"
        commerce_story_plan = plan_commerce_story_llm(
            strategy=strategy,
            cards=commerce_evidence_cards,
            target_duration=target_duration,
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            raw_response_hook=lambda value: commerce_path.write_text(value, encoding="utf-8"),
        )
        commerce_director_response = commerce_path.name
        contract["commerce_director_experiment"] = True
        contract["commerce_story_plan"] = commerce_story_plan.to_dict()
    if commerce_lite or commerce_lite_draft_final or commerce_lite_draft_rank_final or commerce_lite_chapter_compression or commerce_lite_final_editor:
        # Tags are a projection of existing Ledger/M1 facts, never a candidate
        # filter. The draft/final variant is source-only and deliberately
        # separated from the normal UI experiment path.
        commerce_lite_tags = build_commerce_lite_tags(
            strategy=strategy,
            safe_candidates=candidates,
            ledger_assets=assets,
            executable_evidence=executable_evidence_facts,
        )
        if commerce_lite_final_editor and single_pass_director_execution:
            # The Director response already made every semantic decision.
            # Resolve that exact source order once and leave all further
            # checks to existing M3 lineage/safety.
            plan = build_single_pass_director_plan(
                strategy=strategy,
                safe_candidates=candidates,
                target_duration=target_duration,
                selection_contract=contract,
                director_contract=director_strategy_contract,
            )
            for variant_strategy in discovered_strategies:
                if variant_strategy.strategy_id == strategy.strategy_id:
                    continue
                if len(tuple(getattr(variant_strategy, "director_sequence", ()) or ())) < 2:
                    continue
                variant_audit = dict(getattr(variant_strategy, "whole_video_audit", {}) or {})
                variant_product_control = dict(variant_audit.get("product_control") or {})
                variant_product_final = dict(variant_product_control.get("final") or {})
                variant_product_status = str(variant_product_final.get("status") or "")
                # Product relation checks are advisory at this stage.  The AI
                # already authored a complete editable strategy from the same
                # source pool; silently dropping S2/S3 here made a three-plan
                # response look like a one-plan preview.  Keep the full plan
                # and surface the existing product-control audit as a warning
                # in the workbench instead of changing the Director's result.
                if variant_product_status == "conflict":
                    variant_audit.setdefault("program_warnings", []).append(
                        "product_scope_conflict_non_blocking"
                    )
                variant_plan = build_single_pass_director_plan(
                    strategy=variant_strategy,
                    safe_candidates=candidates,
                    target_duration=target_duration,
                    selection_contract=contract,
                    director_contract={
                        **dict(director_strategy_contract or {}),
                        "director_strategy_id": variant_strategy.strategy_id,
                    },
                )
                if variant_plan.selected_candidates:
                    director_variant_plans.append({
                        "strategy": variant_strategy.to_dict(),
                        "m2_plan": variant_plan.to_dict(),
                    })
            commerce_lite_response = "embedded_in_single_ai_director_response"
            commerce_lite_completion_response = "not_run_single_ai_director_packet"
            commerce_strong_clip_ranking_response = "not_run_single_ai_director_packet"
            commerce_purchase_cognition_response = "embedded_in_single_ai_director_response"
            commerce_purchase_journey_recall_response = "not_run_single_ai_director_packet"
            commerce_purchase_journey_quality_response = "not_run_single_ai_director_packet"
            commerce_chapter_packet_response = "not_run_single_ai_director_packet"
            commerce_narrative_enrichment_response = "not_run_single_ai_director_packet"
            commerce_lite_final_editor_response = "not_run_single_ai_director_packet"
            commerce_lite_final_editor_audit = {
                "status": "not_run",
                "reason": (
                    "the two-stage AI director created a full source draft then revised it using measured duration and warnings"
                    if two_pass_director else
                    "the one AI director response already chose the story, journey and exact source beats"
                ),
                "semantic_call_count": 2 if two_pass_director else 1,
                "semantic_call_count_after_m1": 0,
                "m3_render_gate": dict(plan.duration_assessment or {}).get("m3_render_gate"),
            }
        elif commerce_lite_final_editor:
            if commerce_lite_replay_plan is not None:
                raise ValueError("Narrative Mode does not accept a replayed pre-P0.5 plan")
            response_stamp = f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}"
            journey_path = output_dir / f"{response_stamp}.m2.narrative_mode.journey.response.txt"
            casting_path = output_dir / f"{response_stamp}.m2.narrative_mode.beat_casting.response.txt"
            whole_audit_path = output_dir / f"{response_stamp}.m2.narrative_mode.whole_video_audit.response.txt"
            inventory_path = output_dir / f"{response_stamp}.p0_5a2.beat_inventory.json"
            actor_pool_path = output_dir / f"{response_stamp}.p0_5a3.actor_pool.json"
            opening_package_path = output_dir / f"{response_stamp}.p0_5a4.opening_packages.json"
            discovery_paths: dict[str, Path] = {}
            boundary_paths: dict[str, Path] = {}
            adjudication_paths: dict[str, Path] = {}
            short_recall_paths: dict[str, Path] = {}
            short_adjudication_paths: dict[str, Path] = {}
            hook_recall_paths: dict[str, Path] = {}

            def _write_batch(kind: str, batch_id: str, value: str) -> None:
                paths = {
                    "discovery": discovery_paths,
                    "boundary": boundary_paths,
                    "adjudication": adjudication_paths,
                }[kind]
                path = output_dir / f"{response_stamp}.p0_5a2.{kind}.{batch_id}.response.txt"
                path.write_text(value, encoding="utf-8")
                paths[batch_id] = path

            if narrative_mode_inventory_override is not None:
                raise ValueError(
                    "P0.5A.2 inventory override cannot enter live Narrative Mode; "
                    "P0.5A.3 requires same-run frozen discovery receipts."
                )
            narrative_source_rows = build_micro_beat_source_rows(
                source_units=ledger_context.get("source_context_units") or (),
                hard_safe_subtitle_ids=[int(item.get("candidate_id") or 0) for item in assets],
                word_timeline=timeline,
            )
            narrative_mode_beat_inventory = build_narrative_mode_beat_inventory(
                source_units=ledger_context.get("source_context_units") or (),
                hard_safe_subtitle_ids=[int(item.get("candidate_id") or 0) for item in assets],
                word_timeline=timeline,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                discovery_response_hook=lambda batch_id, value: _write_batch("discovery", batch_id, value),
                boundary_response_hook=lambda batch_id, value: _write_batch("boundary", batch_id, value),
                adjudication_response_hook=lambda batch_id, value: _write_batch("adjudication", batch_id, value),
            )
            inventory_path.write_text(
                json.dumps(narrative_mode_beat_inventory, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            def _write_short_recall(batch_id: str, value: str) -> None:
                path = output_dir / f"{response_stamp}.p0_5a3.boundary.{batch_id}.response.txt"
                path.write_text(value, encoding="utf-8")
                short_recall_paths[batch_id] = path

            def _write_short_adjudication(batch_id: str, value: str) -> None:
                path = output_dir / f"{response_stamp}.p0_5a3.adjudication.{batch_id}.response.txt"
                path.write_text(value, encoding="utf-8")
                short_adjudication_paths[batch_id] = path

            narrative_mode_p05a3_actor_pool = _build_p05a3_runtime_actor_pool(
                p05a2_inventory=narrative_mode_beat_inventory,
                discovery_responses=[path.read_text(encoding="utf-8") for _key, path in sorted(discovery_paths.items())],
                source_rows=narrative_source_rows,
                settings=settings,
                boundary_response_hook=_write_short_recall,
                adjudication_response_hook=_write_short_adjudication,
            )
            if narrative_mode_p05a3_actor_pool.get("status") != "publishable_inventory_calibrated":
                raise ValueError(
                    "P0.5A.3 Actor Pool unavailable: "
                    + ";".join(narrative_mode_p05a3_actor_pool.get("errors") or ())
                )
            actor_pool_path.write_text(
                json.dumps(narrative_mode_p05a3_actor_pool, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            narrative_mode_beat_execution, narrative_mode_beat_candidates = _narrative_candidates_from_execution(
                inventory=narrative_mode_p05a3_actor_pool,
                execution=prepare_narrative_mode_beat_execution(
                    publishable_inventory=narrative_mode_p05a3_actor_pool, word_timeline=timeline,
                ),
            )
            if not narrative_mode_beat_candidates:
                raise ValueError("P0.5A.3 没有可供 Narrative Mode Beat Casting 的 clean/visual 词级 Beat")

            def _opening_package_provider(journey: Mapping[str, Any]) -> Mapping[str, Any]:
                """Expose AI-approved P0.5A.4 packages to the existing Beat Caster.

                The provider deliberately does not pick a package.  It scans
                the complete safe source for Hooks, has AI pair each one to
                the frozen A.3 actor pool, then gives every valid pairing to
                the semantic Beat Caster as its only legal opening choices.
                """
                nonlocal narrative_mode_beat_execution, narrative_mode_beat_candidates
                nonlocal narrative_mode_hook_recall, narrative_mode_opening_packages
                opening_scope = dict((director_strategy_contract or {}).get("opening_scope") or {})
                allowed_roles = [
                    str(value).strip().lower() for value in opening_scope.get("allowed_answer_roles") or ()
                    if str(value).strip()
                ] or ["result", "proof"]

                def _write_hook_response(batch_id: str, value: str) -> None:
                    path = output_dir / f"{response_stamp}.p0_5a4.hook.{batch_id}.response.txt"
                    path.write_text(value, encoding="utf-8")
                    hook_recall_paths[batch_id] = path

                narrative_mode_hook_recall = recall_hooks_from_complete_source(
                    source_rows=narrative_source_rows,
                    api_key=str(settings["api_key"]),
                    base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                    model=str(settings.get("model") or "deepseek-v4-flash"),
                    opening_promise=str(journey.get("opening_promise") or ""),
                    allowed_opening_answer_roles=allowed_roles,
                    response_hook=_write_hook_response,
                )
                narrative_mode_opening_packages = assemble_opening_packages(
                    hook_recall=narrative_mode_hook_recall,
                    frozen_actor_pool=_mapping_rows(narrative_mode_p05a3_actor_pool.get("publishable_beat_inventory")),
                    api_key=str(settings["api_key"]),
                    base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                    model=str(settings.get("model") or "deepseek-v4-flash"),
                    opening_promise=str(journey.get("opening_promise") or ""),
                    response_hook=lambda value: _write_hook_response("OP01", value),
                )
                raw_packages = [
                    item for item in _mapping_rows(narrative_mode_opening_packages.get("opening_packages"))
                    if str(item.get("quality") or "").lower() in {"strong", "medium"}
                ]
                hook_by_id = {
                    str(item.get("hook_id") or ""): item
                    for item in _mapping_rows(narrative_mode_hook_recall.get("hook_candidates"))
                }
                overlays: list[dict[str, Any]] = []
                overlay_errors: list[str] = []
                for hook_id in sorted({str(item.get("hook_id") or "") for item in raw_packages}):
                    try:
                        overlays.append(hook_candidate_as_opening_overlay(hook_by_id[hook_id]))
                    except (KeyError, ValueError) as error:
                        overlay_errors.append(f"p0_5a4_hook_overlay_invalid:{hook_id}:{error}")
                combined_inventory = {
                    "publishable_beat_inventory": (
                        _mapping_rows(narrative_mode_p05a3_actor_pool.get("publishable_beat_inventory")) + overlays
                    ),
                }
                expanded_execution, expanded_candidates = _narrative_candidates_from_execution(
                    inventory=combined_inventory,
                    execution=prepare_narrative_mode_beat_execution(
                        publishable_inventory=combined_inventory, word_timeline=timeline,
                    ),
                    disable_non_hook_openings=True,
                )
                candidate_id_by_beat_id = {
                    str(item.get("beat_id") or ""): int(item.get("candidate_id") or 0)
                    for item in expanded_execution.get("beat_candidate_map") or () if isinstance(item, Mapping)
                }
                approved_packages: list[dict[str, Any]] = []
                for package in raw_packages:
                    hook_id = str(package.get("hook_id") or "")
                    payoff_ids = [str(item) for item in package.get("payoff_beat_ids") or () if str(item)]
                    hook_candidate_id = candidate_id_by_beat_id.get(hook_id, 0)
                    payoff_candidate_ids = [candidate_id_by_beat_id.get(item, 0) for item in payoff_ids]
                    if not hook_candidate_id or not payoff_candidate_ids or any(not value for value in payoff_candidate_ids):
                        overlay_errors.append(f"p0_5a4_opening_package_lineage_missing:{package.get('opening_id')}")
                        continue
                    approved_packages.append({
                        **package,
                        "hook_candidate_id": hook_candidate_id,
                        "payoff_candidate_ids": payoff_candidate_ids,
                    })
                narrative_mode_opening_packages = dict(narrative_mode_opening_packages) | {
                    "casting_opening_packages": approved_packages,
                    "hook_overlay_count": len(overlays),
                    "actor_pool_unchanged": True,
                }
                opening_package_path.write_text(
                    json.dumps({
                        "hook_recall": narrative_mode_hook_recall,
                        "opening_packages": narrative_mode_opening_packages,
                    }, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                if approved_packages:
                    narrative_mode_beat_execution = expanded_execution
                    narrative_mode_beat_candidates = expanded_candidates
                archetype = str((director_strategy_contract or {}).get("narrative_archetype") or "")
                unavailable_reason = (
                    "narrative_archetype_unavailable:no_clean_scope_compliant_scene_opening_package"
                    if archetype == "scene_immersion" else "p0_5a4_opening_package_material_limited"
                )
                return {
                    "beat_candidates": expanded_candidates,
                    "safe_candidates": expanded_execution.get("candidates") or (),
                    "approved_opening_packages": approved_packages,
                    "errors": overlay_errors if overlay_errors else ([] if approved_packages else [unavailable_reason]),
                    "audit": {
                        "hook_recall_status": narrative_mode_hook_recall.get("status"),
                        "opening_package_status": narrative_mode_opening_packages.get("status"),
                        "approved_package_count": len(approved_packages),
                        "actor_pool_unchanged": True,
                        "selection_authority": "AI_P0_5A4_package_then_AI_beat_casting",
                    },
                }

            narrative_mode_audit, plan = plan_commerce_lite_narrative_mode_llm(
                strategy=strategy,
                beat_candidates=narrative_mode_beat_candidates,
                target_duration=target_duration,
                safe_candidates=narrative_mode_beat_execution["candidates"],
                selection_contract=contract,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                director_strategy_contract=director_strategy_contract,
                opening_package_provider=_opening_package_provider,
                journey_response_hook=lambda value: journey_path.write_text(value, encoding="utf-8"),
                casting_response_hook=lambda value: casting_path.write_text(value, encoding="utf-8"),
                whole_video_audit_response_hook=lambda value: whole_audit_path.write_text(value, encoding="utf-8"),
            )
            narrative_mode_response_paths = {
                "inventory": inventory_path.name,
                "inventory_mode": "fresh_complete_source_p0_5a2_then_p0_5a3_actor_pool",
                "p0_5a3_actor_pool": actor_pool_path.name,
                "discovery": {key: value.name for key, value in discovery_paths.items()},
                "boundary": {key: value.name for key, value in boundary_paths.items()},
                "adjudication": {key: value.name for key, value in adjudication_paths.items()},
                "p0_5a3_short_recall_boundary": {key: value.name for key, value in short_recall_paths.items()},
                "p0_5a3_adjudication": {key: value.name for key, value in short_adjudication_paths.items()},
                "p0_5a4_hook_recall": {key: value.name for key, value in hook_recall_paths.items()},
                "p0_5a4_opening_packages": opening_package_path.name if opening_package_path.exists() else "not_completed",
                "journey": journey_path.name if journey_path.exists() else "not_completed",
                "beat_casting": casting_path.name if casting_path.exists() else "not_completed",
                "whole_video_audit": whole_audit_path.name if whole_audit_path.exists() else "not_completed",
            }
            commerce_lite_response = narrative_mode_response_paths["journey"]
            commerce_lite_completion_response = "not_used_narrative_mode_journey_is_director_first"
            commerce_strong_clip_ranking_response = "bypassed_p0_5a3_actor_pool_plus_p0_5a4_hook_overlay_are_the_only_beat_casting_input"
            commerce_purchase_cognition_response = narrative_mode_response_paths["journey"]
            commerce_purchase_journey_recall_response = "not_used_full_p0_5a3_inventory_visible_to_beat_casting"
            commerce_purchase_journey_quality_response = narrative_mode_response_paths["whole_video_audit"]
            commerce_chapter_packet_response = "not_used_narrative_mode"
            commerce_narrative_enrichment_response = "not_used_narrative_mode"
            commerce_lite_final_editor_response = "replaced_by_p0_5a3_actor_pool_p0_5a4_opening_package_narrative_mode"
            commerce_lite_final_editor_audit = narrative_mode_audit
        elif commerce_lite_chapter_compression:
            if commerce_lite_replay_plan is not None:
                raise ValueError("Chapter Compression experiment does not accept a replayed Lite plan")
            initial_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.initial.response.txt"
            initial_completion_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.initial.completion.response.txt"
            compression_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_4.chapter_compression.response.txt"
            commerce_lite_source_plan = plan_commerce_lite_llm(
                strategy=strategy,
                tags=commerce_lite_tags,
                target_duration=target_duration,
                safe_candidates=candidates,
                selection_contract=contract,
                executable_evidence=executable_evidence_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                raw_response_hook=lambda value: initial_path.write_text(value, encoding="utf-8"),
                completion_response_hook=lambda value: initial_completion_path.write_text(value, encoding="utf-8"),
            )
            plan = plan_commerce_lite_chapter_compression_llm(
                strategy=strategy,
                current_plan=commerce_lite_source_plan,
                tags=commerce_lite_tags,
                target_duration=target_duration,
                safe_candidates=candidates,
                selection_contract=contract,
                executable_evidence=executable_evidence_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                response_hook=lambda value: compression_path.write_text(value, encoding="utf-8"),
            )
            commerce_lite_response = initial_path.name
            commerce_lite_completion_response = (
                initial_completion_path.name if initial_completion_path.exists() else "not_attempted_or_no_completion_response"
            )
            commerce_lite_compression_response = compression_path.name
        elif commerce_lite_draft_rank_final:
            if commerce_lite_replay_plan is not None:
                raise ValueError("Draft -> Ranking -> Final experiment does not accept a replayed final plan")
            draft_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.draft.response.txt"
            ranking_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.ranking.response.txt"
            final_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.final.response.txt"
            commerce_lite_draft, commerce_lite_ranking, plan = plan_commerce_lite_draft_rank_final_llm(
                strategy=strategy,
                tags=commerce_lite_tags,
                target_duration=target_duration,
                safe_candidates=candidates,
                selection_contract=contract,
                executable_evidence=executable_evidence_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                draft_response_hook=lambda value: draft_path.write_text(value, encoding="utf-8"),
                ranking_response_hook=lambda value: ranking_path.write_text(value, encoding="utf-8"),
                final_response_hook=lambda value: final_path.write_text(value, encoding="utf-8"),
            )
            commerce_lite_draft_response = draft_path.name
            commerce_lite_ranking_response = ranking_path.name
            commerce_lite_response = final_path.name
            commerce_lite_completion_response = "not_used_draft_ranking_final_experiment"
        elif commerce_lite_draft_final:
            if commerce_lite_replay_plan is not None:
                raise ValueError("Draft -> Final experiment does not accept a replayed final plan")
            draft_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.draft.response.txt"
            final_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.final.response.txt"
            commerce_lite_draft, plan = plan_commerce_lite_draft_final_llm(
                strategy=strategy,
                tags=commerce_lite_tags,
                target_duration=target_duration,
                safe_candidates=candidates,
                selection_contract=contract,
                executable_evidence=executable_evidence_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                draft_response_hook=lambda value: draft_path.write_text(value, encoding="utf-8"),
                final_response_hook=lambda value: final_path.write_text(value, encoding="utf-8"),
            )
            commerce_lite_draft_response = draft_path.name
            commerce_lite_response = final_path.name
            commerce_lite_completion_response = "not_used_draft_final_experiment"
        elif commerce_lite_replay_plan is not None:
            plan = commerce_lite_replay_plan
            commerce_lite_response = "replayed_existing_lite_plan_no_model_call"
            commerce_lite_completion_response = "not_applicable_replayed_existing_lite_plan"
        else:
            lite_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.response.txt"
            completion_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2_5.lite.completion.response.txt"
            plan = plan_commerce_lite_llm(
                strategy=strategy,
                tags=commerce_lite_tags,
                target_duration=target_duration,
                safe_candidates=candidates,
                selection_contract=contract,
                executable_evidence=executable_evidence_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                raw_response_hook=lambda value: lite_path.write_text(value, encoding="utf-8"),
                completion_response_hook=lambda value: completion_path.write_text(value, encoding="utf-8"),
            )
            commerce_lite_response = lite_path.name
            commerce_lite_completion_response = (
                completion_path.name if completion_path.exists() else "not_attempted_or_no_completion_response"
            )
        commerce_lite_raw_plan = plan
        if narrative_mode_beat_execution is None and not single_pass_director_execution:
            plan, commerce_lite_execution_adapter = align_lite_execution_metadata(
                plan=plan,
                strategy=strategy,
                safe_candidates=candidates,
                executable_evidence=executable_evidence_facts,
            )
        m2_attempts: list[dict[str, Any]] = [{
            "kind": (
                "two_pass_director_packet_exact_source_materialization"
                if single_pass_director_execution and two_pass_director else
                "single_ai_director_packet_exact_source_materialization"
                if single_pass_director_execution else "p0_5a3_actor_pool_p0_5a4_opening_package_journey_then_beat_casting"
                if commerce_lite_final_editor else "commerce_lite_chapter_compression"
                if commerce_lite_chapter_compression else "commerce_lite_draft_ranking_final"
                if commerce_lite_draft_rank_final else "commerce_lite_draft_to_final"
                if commerce_lite_draft_final else "commerce_lite_execution_adapter_replay"
                if commerce_lite_replay_plan is not None else "commerce_lite_rank_plus_bounded_completion"
            ),
            "response": commerce_lite_response,
            "draft_response": commerce_lite_draft_response,
            "ranking_response": commerce_lite_ranking_response,
            "completion_response": commerce_lite_completion_response,
            "chapter_compression_response": commerce_lite_compression_response,
            "final_editor_response": commerce_lite_final_editor_response,
            "final_editor_audit": commerce_lite_final_editor_audit,
            "strong_clip_ranking_response": commerce_strong_clip_ranking_response,
            "purchase_cognition_response": commerce_purchase_cognition_response,
            "purchase_journey_targeted_recall_response": commerce_purchase_journey_recall_response,
            "purchase_journey_quality_response": commerce_purchase_journey_quality_response,
            "chapter_packet_response": commerce_chapter_packet_response,
            "narrative_enrichment_response": commerce_narrative_enrichment_response,
            "strong_clip_ranking": [item.to_dict() for item in commerce_strong_clip_ranking],
            "source_plan": commerce_lite_source_plan.to_dict() if commerce_lite_source_plan else None,
            "raw_plan": commerce_lite_raw_plan.to_dict(),
            "plan": plan.to_dict(),
        }]
    else:
        plan = plan_narrative_llm(
            strategy=strategy,
            target_duration=target_duration,
            safe_candidates=candidates,
            selection_contract=contract,
            executable_evidence=executable_evidence_facts,
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            raw_response_hook=lambda value: m2_path.write_text(value, encoding="utf-8"),
        )
        m2_attempts = [{
            "kind": "initial",
            "response": m2_path.name,
            "plan": plan.to_dict(),
        }]
        for attempt in range(1, max(0, int(m2_replan_attempts)) + 1):
            if plan.plan_valid or not plan.replan_request:
                break
            replan_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2.replan{attempt}.response.txt"
            plan = replan_narrative_llm(
                strategy=strategy,
                invalid_plan=plan,
                safe_candidates=candidates,
                selection_contract=contract,
                executable_evidence=executable_evidence_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                raw_response_hook=lambda value, path=replan_path: path.write_text(value, encoding="utf-8"),
            )
            m2_attempts.append({
                "kind": f"replan{attempt}",
                "response": replan_path.name,
                "plan": plan.to_dict(),
            })
    opening_review_attempt: dict[str, Any] | None = None
    # Purchase Journey Quality already owns the opening decision inside the
    # same M2 pass (where it has the local Q1 alternatives and cleanliness
    # evidence).  The legacy post-plan opening review is a separate model
    # replan with an older M1-only opening whitelist; running it here can
    # replace a passed Q1/Q2 pair after the three M2 quality receipts exist.
    # Keep that legacy review for the older route, but never let it rewrite a
    # Purchase Journey Quality plan between M2 approval and M3 materializing.
    if single_pass_director_execution:
        opening_review_attempt = {
            "status": "skipped",
            "reason": "opening was selected in the single AI director packet; no second semantic review is allowed",
        }
    elif commerce_lite_final_editor:
        opening_review_attempt = {
            "status": "skipped",
            "reason": "narrative_mode_whole_video_audit_owns_opening_quality_before_m3",
        }
    elif opening_quality_review and plan.plan_valid:
        review_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.m2.opening_review.response.txt"
        plan = review_opening_quality_llm(
            strategy=strategy,
            plan=plan,
            safe_candidates=candidates,
            executable_evidence=executable_evidence_facts,
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            raw_response_hook=lambda value, path=review_path: path.write_text(value, encoding="utf-8"),
        )
        opening_review_attempt = {
            "response": review_path.name,
            "review": plan.opening_quality_review.to_dict() if plan.opening_quality_review else None,
            "plan": plan.to_dict(),
        }
    # This only makes duration reporting honest after the director has used its
    # allotted M2 retries.  It never changes candidates, chapters, order, or
    # M3 behaviour, so short-but-faithful materialization remains a pass for
    # fidelity and a failure only for the separate duration target.
    plan = finalize_duration_budget_after_retry(plan)
    execution_candidates = (
        narrative_mode_beat_execution["candidates"]
        if narrative_mode_beat_execution is not None else candidates
    )
    consumption = audit_story_consumption(plan, strategy, execution_candidates)
    commerce_coverage = (
        audit_commerce_story_coverage(plan, commerce_story_plan)
        if commerce_story_plan is not None else None
    )
    quality_gate = (
        dict(plan.duration_assessment or {}).get("m3_render_gate", {})
        if single_pass_director_execution or narrative_mode_beat_execution is not None else
        dict(plan.duration_assessment or {})
        .get("commerce_purchase_journey_quality", {})
        .get("m3_render_gate", {})
    )
    if skip_m3_materialization:
        candidate_words = {}
        candidate_word_rejections = ()
        fidelity = {
            "status": "not_run_sentence_preview",
            "passed": False,
            "issues": [],
            "reason": "M3 was intentionally bypassed; M2 source sentences go directly to the editable preview.",
        }
        binder = {
            "status": "not_required_sentence_preview",
            "coverage": None,
            "ambiguous": 0,
            "unmatched": 0,
            "validation_issues": [],
        }
        result_payload = {
            "status": "not_run_sentence_preview",
            "ranges": [],
            "reason": "M3 word materialization intentionally disabled for this preview route.",
        }
        chapter_lineage: list[dict[str, Any]] = []
        render_manifest = None
        passed = bool(plan.plan_valid and plan.selected_candidates)
    elif narrative_mode_beat_execution is not None:
        candidate_words = dict(narrative_mode_beat_execution["candidate_words"])
        bound_ids = set(candidate_words)
        candidate_word_rejections = tuple(
            f"p0_5a2_selected_beat_word_binding_missing:{candidate.candidate_id}"
            for candidate in plan.selected_candidates if candidate.candidate_id not in bound_ids
        )
        fidelity_ledger = narrative_mode_beat_execution["execution_ledger"]
    else:
        candidate_words, candidate_word_rejections = bind_candidates_by_semantic_srt(plan.selected_candidates, timeline)
        fidelity_ledger = assets
    if not skip_m3_materialization:
        result = materialize_narrative_plan(plan, candidate_words)
        result_payload = result.to_dict()
        fidelity = audit_materialization_fidelity(
            plan, result, fidelity_ledger, require_word_boundaries=True,
        )
        binder = timeline.report()
        binder_passed = bool(
            binder["coverage"] == 1.0
            and binder["ambiguous"] == 0
            and binder["unmatched"] == 0
            and not binder["validation_issues"]
            and not candidate_word_rejections
        )
        quality_gate_passed = bool(quality_gate.get("passed")) if commerce_lite_final_editor else True
        passed = bool(
            plan.plan_valid
            and quality_gate_passed
            and consumption["passed"]
            and binder_passed
            and fidelity["passed"]
        )
        render_manifest = _write_approved_selector_render_manifest(
            output_dir=output_dir,
            case_id=case_id,
            source_srt=str(ledger_context["source_srt"]),
            plan=plan,
            selector_result=result_payload,
            quality_gate=quality_gate,
            approved=passed,
        )
        chapter_lineage = _chapter_trace(plan, result, timeline)
    opening_review = plan.opening_quality_review
    return {
        "label": label,
        "product": product,
        "source_srt": ledger_context["source_srt"],
        "m1_response": "reused_frozen_m1_strategy" if m1_result is None else m1_path.name,
        "m1_result": (
            {"reused_frozen_same_source_strategy": strategy.to_dict()}
            if m1_result is None else m1_result.to_dict()
        ),
        "two_pass_director_responses": dict(two_pass_response_paths),
        "director_controls": effective_director_controls,
        "selected_m1_hero": strategy.to_dict(),
        "m1_hero_selection_reason": hero_selection_reason,
        "m1_story_library": story_library,
        "director_strategy_library": director_strategy_library,
        "director_variant_plans": director_variant_plans,
        "director_strategy_contract": dict(director_strategy_contract or {}),
        "m2_attempts": m2_attempts,
        "m2_opening_quality_review_attempt": opening_review_attempt,
        "m2_purchase_journey_quality_render_gate": quality_gate if commerce_lite_final_editor else None,
        "p0_5a2_narrative_mode_beat_inventory": narrative_mode_beat_inventory,
        "p0_5a3_narrative_mode_actor_pool": narrative_mode_p05a3_actor_pool,
        "p0_5a4_narrative_mode_hook_recall": narrative_mode_hook_recall,
        "p0_5a4_narrative_mode_opening_packages": narrative_mode_opening_packages,
        "p0_5a2_narrative_mode_execution": (
            {
                "beat_candidate_map": narrative_mode_beat_execution["beat_candidate_map"],
                "binding_rejections": narrative_mode_beat_execution["binding_rejections"],
                "contract": narrative_mode_beat_execution["contract"],
            }
            if narrative_mode_beat_execution is not None else None
        ),
        "p0_5a3_p0_5a4_narrative_mode_execution": (
            {
                "beat_candidate_map": narrative_mode_beat_execution["beat_candidate_map"],
                "binding_rejections": narrative_mode_beat_execution["binding_rejections"],
                "contract": narrative_mode_beat_execution["contract"],
                "hook_overlay_only_opening_permission": True,
            }
            if narrative_mode_beat_execution is not None else None
        ),
        "p0_5a2_narrative_mode_responses": narrative_mode_response_paths,
        "opening_quality_review_completed": opening_review is not None,
        "opening_quality_review_requires_human_audit": True,
        "m2_plan": plan.to_dict(),
        "m2_story_consumption_audit": consumption,
        "commerce_director": (
            {
                "enabled": True,
                "response": commerce_director_response,
                "evidence_cards": [card.to_dict() for card in commerce_evidence_cards],
                "commerce_story_plan": commerce_story_plan.to_dict(),
                "coverage_audit": commerce_coverage,
                "boundary": "pre_selection_coverage_contract_not_candidate_filter",
            }
            if commerce_story_plan is not None else {"enabled": False}
        ),
        "commerce_lite": (
            {
                "enabled": True,
                "response": commerce_lite_response,
                "draft_response": commerce_lite_draft_response,
                "ranking_response": commerce_lite_ranking_response,
                "completion_response": commerce_lite_completion_response,
                "chapter_compression_response": commerce_lite_compression_response,
                "final_editor_response": commerce_lite_final_editor_response,
                "final_editor_audit": commerce_lite_final_editor_audit,
                "strong_clip_ranking_response": commerce_strong_clip_ranking_response,
                "purchase_cognition_response": commerce_purchase_cognition_response,
                "purchase_journey_targeted_recall_response": commerce_purchase_journey_recall_response,
                "purchase_journey_quality_response": commerce_purchase_journey_quality_response,
                "chapter_packet_response": commerce_chapter_packet_response,
                "narrative_enrichment_response": commerce_narrative_enrichment_response,
                "narrative_mode_responses": narrative_mode_response_paths,
                "narrative_mode_audit": narrative_mode_audit,
                "strong_clip_ranking": [item.to_dict() for item in commerce_strong_clip_ranking],
                "draft": commerce_lite_draft.to_dict() if commerce_lite_draft else None,
                "commercial_ranking": commerce_lite_ranking.to_dict() if commerce_lite_ranking else None,
                "source_plan": commerce_lite_source_plan.to_dict() if commerce_lite_source_plan else None,
                "tags": [tag.to_dict() for tag in commerce_lite_tags],
                "raw_plan": commerce_lite_raw_plan.to_dict() if commerce_lite_raw_plan else {},
                "execution_adapter": (
                    commerce_lite_execution_adapter.to_dict() if commerce_lite_execution_adapter else {}
                ),
                "tagging_mode": (
                    "p0_5a3_clean_visual_actor_pool_plus_p0_5a4_hook_overlay_no_candidate_tag_ranking"
                    if narrative_mode_beat_execution is not None else
                    "existing_ledger_and_m1_projection_no_new_llm_call"
                ),
                "ranking_mode": (
                    "director_core_desire_and_journey_then_p0_5a3_actor_pool_p0_5a4_opening_package_casting_no_strong_top12_v2"
                    if commerce_lite_final_editor else "initial_lite_then_same_chapter_compression_with_bounded_new_purchase_value_append"
                    if commerce_lite_chapter_compression else "candidate_free_draft_then_commercial_value_ranking_then_strict_final_no_replan_loop"
                    if commerce_lite_draft_rank_final else "candidate_free_draft_then_strict_final_no_replan_loop"
                    if commerce_lite_draft_final else "initial_rank_plus_bounded_append_only_completion_when_needed"
                ),
                "boundary": (
                    "P0_5A3_clean_visual_actor_pool_plus_P0_5A4_AI_approved_opening_packages_are_only_beat_casting_input"
                    if narrative_mode_beat_execution is not None else
                    "complete_materializable_hard_safe_pool_no_local_filter_or_rank"
                ),
            }
            if commerce_lite or commerce_lite_draft_final or commerce_lite_draft_rank_final or commerce_lite_chapter_compression or commerce_lite_final_editor else {"enabled": False}
        ),
        "hard_safe_candidate_count": ledger_context["hard_safe_candidate_count"],
        "m2_materializable_hard_safe_candidate_count": len(candidates),
        "m2_narrative_mode_p0_5a2_beat_candidate_count": len(narrative_mode_beat_candidates),
        "m2_materializability_preflight": {
            "blocked": materializability_blocked,
            "candidate_word_lineage_rejections": list(all_candidate_word_rejections),
            "ledger_is_unchanged": True,
        },
        "m2_executable_evidence_view": [
            item.to_dict()
            for item in build_executable_evidence_view(
                CommercialStoryBrief.from_strategy(strategy), execution_candidates,
                (
                    {
                        candidate.candidate_id: {"materializable": True, "origin": "p0_5a2_boundary_word_exact"}
                        for candidate in execution_candidates
                    }
                    if narrative_mode_beat_execution is not None else executable_evidence_facts
                ),
            )
        ],
        "semantic_binder": binder,
        "candidate_word_binding": {
            "bound_candidate_ids": sorted(candidate_words),
            "rejections": list(candidate_word_rejections),
        },
        "m3_selection_result": result_payload,
        "m3_plan_fidelity_audit": fidelity,
        "chapter_lineage": chapter_lineage,
        "approved_selector_render_manifest": str(render_manifest) if render_manifest else None,
        "rendered_video": False,
        "passed": passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(M3_GOLDEN_SOURCES) + ("all",), default="all")
    parser.add_argument("--strategy-id", default="", help="Fix one M1 strategy ID for a one-case rerun")
    parser.add_argument("--target-duration", type=float, default=40.0)
    parser.add_argument("--duration-tolerance", type=float, default=None)
    parser.add_argument("--m2-replan-attempts", type=int, default=1)
    parser.add_argument("--no-opening-quality-review", action="store_true")
    parser.add_argument(
        "--frozen-m1-strategy-report", default="",
        help="Reuse selected_m1_hero from a same-source experiment report; do not call M1 again.",
    )
    parser.add_argument(
        "--purchase-journey-quality", action="store_true",
        help="Run P0.5A.2 Narrative Mode: Director Journey -> full Beat Casting -> Whole Video Audit.",
    )
    parser.add_argument(
        "--narrative-archetype", choices=("pain_point", "scene_immersion"), default="",
        help="Attach the existing P0.1 Director Blueprint before P0.5A.2 Beat Casting.",
    )
    parser.add_argument(
        "--source-srt", default="",
        help="Override the golden-case SRT for a same-source frozen-M1 M2 regression.",
    )
    parser.add_argument("--source-label", default="")
    parser.add_argument("--source-product", default="")
    parser.add_argument(
        "--p0-5a2-inventory", default="",
        help="Reuse one same-source P0.5A.2 inventory for a focused Journey/Casting/M3 regression; normal Narrative Mode always rebuilds it.",
    )
    parser.add_argument("--out-dir", default="workspace/m3_new_golden_plan_fidelity")
    args = parser.parse_args()
    from ai_clipper import load_settings  # noqa: E402

    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行真实 M1→M2→M3 原型验证。")
    case_ids = tuple(M3_GOLDEN_SOURCES) if args.case == "all" else (args.case,)
    if args.strategy_id and len(case_ids) != 1:
        raise ValueError("--strategy-id 只能用于单案例重跑")
    if args.frozen_m1_strategy_report and len(case_ids) != 1:
        raise ValueError("--frozen-m1-strategy-report 只能用于单案例重跑")
    if args.source_srt and len(case_ids) != 1:
        raise ValueError("--source-srt 只能用于单案例重跑")
    if args.p0_5a2_inventory and len(case_ids) != 1:
        raise ValueError("--p0-5a2-inventory 只能用于单案例重跑")
    if args.narrative_archetype and not args.purchase_journey_quality:
        raise ValueError("--narrative-archetype 仅用于现有 --purchase-journey-quality M2 路径")
    frozen_m1_strategy = (
        _load_frozen_m1_strategy(args.frozen_m1_strategy_report)
        if args.frozen_m1_strategy_report else None
    )
    narrative_mode_inventory_override = (
        json.loads(Path(args.p0_5a2_inventory).read_text(encoding="utf-8"))
        if args.p0_5a2_inventory else None
    )
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "version": "m3-new-golden-plan-fidelity-v1",
        "mode": (
            "frozen_m1_purchase_journey_quality_m2_source_only_m3_no_preview_no_render"
            if frozen_m1_strategy is not None and args.purchase_journey_quality
            else "live_m1_m2_source_only_m3_no_preview_no_render"
        ),
        "m1_m2_m3_formal_paths_changed": False,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": {},
    }
    for case_id in case_ids:
        source_definition = (
            {
                "srt": str(Path(args.source_srt).resolve()),
                "label": str(args.source_label or case_id),
                "product": str(args.source_product or M3_GOLDEN_PRODUCTS.get(case_id) or case_id),
            }
            if args.source_srt else None
        )
        item = _run_case(
            case_id,
            settings=settings,
            output_dir=output_dir,
            target_duration=args.target_duration,
            duration_tolerance=args.duration_tolerance,
            strategy_id="" if frozen_m1_strategy is not None else args.strategy_id,
            m2_replan_attempts=args.m2_replan_attempts,
            # The Quality route contains its opening-quality judgement inside
            # M2.  Do not apply the legacy post-plan rewriter after it.
            opening_quality_review=(
                not args.no_opening_quality_review
                and not args.purchase_journey_quality
            ),
            commerce_lite_final_editor=args.purchase_journey_quality,
            m1_strategy_override=frozen_m1_strategy,
            narrative_archetype=args.narrative_archetype,
            narrative_mode_inventory_override=narrative_mode_inventory_override,
            source_definition=source_definition,
        )
        report["cases"][case_id] = item
        fidelity = item["m3_plan_fidelity_audit"]
        print(
            f"[M3 New Golden] {case_id}: plan={item['m2_plan']['plan_valid']} "
            f"consumption={item['m2_story_consumption_audit']['passed']} "
            f"fidelity={fidelity['passed']} passed={item['passed']}"
        )
    passed_count = sum(bool(item["passed"]) for item in report["cases"].values())
    report["summary"] = {
        "passed": passed_count,
        "expected": len(case_ids),
        "all_passed": passed_count == len(case_ids),
        # M3 Fidelity is an execution result.  Even a semantic M2 opening
        # review remains audit evidence, not an automatic commercial-quality
        # approval; the caller must inspect the returned text before blind eval.
        "blind_eval_ready": False,
        "blind_eval_block_reason": "opening_quality_review_requires_human_audit",
        "shadow_ready": False,
    }
    output_path = output_dir / f"m3_new_golden_plan_fidelity_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M3 New Golden] {passed_count}/{len(case_ids)} report={output_path}")
    raise SystemExit(0 if report["summary"]["all_passed"] else 2)


if __name__ == "__main__":
    main()
